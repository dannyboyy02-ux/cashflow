"""Stamp expected_collection_date on each open SO line via a collection-lag waterfall.

The open-sales-order analogue of src.calc.receipts_timing. An open SO line becomes
a receivable when it ships (plannedShipmentDate), then collects after a lag that
reflects the customer's payment behavior. For each line:

    raw_collection_date      = plannedShipmentDate + collection_lag
    expected_collection_date = max(raw_collection_date, as_of_date)   (overdue clamp)

COLLECTION-LAG WATERFALL (3 tiers, resolved per customer from bc_customer_dso):

  Tier 1  dso_method == 'ratio'                          -> lag = dso_days
          The customer carries open AR, so the empirical DSO ratio reflects how
          they actually pay. so_timing_method = 'so_dso_ratio'.

  Tier 2  customer present, dso_method in               -> lag = terms_days
          ('no_balance', 'terms_fallback')
          No payment history to measure, so use the customer card's net terms
          (resolved upstream into bc_customer_dso.terms_days). This is the
          dominant case for SO customers, who frequently have no open AR.
          so_timing_method = 'so_terms'.

  Tier 3  customer absent from bc_customer_dso entirely  -> lag = DEFAULT_DUE_DAYS
          No card on file at all; fall back to the house-standard NET30.
          so_timing_method = 'so_default'.

Rationale: previously the SO side reused dso_days directly, so the ~3,937 of
3,958 lines whose customers have no open AR resolved with dso_days == 0 (a
'no_balance' DSO), i.e. zero collection lag -- every SO treated as collected the
day it ships. The waterfall applies card terms in that case instead, moving SO
receipts to a realistic later week.

Differences from receipts_timing (unchanged from before): no documentType filter
(SO lines are Item-only), source_stream = "open_so", and the value carried
forward is outstandingAmount (not "amount").

Output table: so_open_with_expected_collection (same name/shape as before, plus
the new so_timing_method column). The bucketing layer maps
expected_collection_date to forecast week 1-13 and aggregates outstandingAmount.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Optional

import pandas as pd

from src.config import LOG_LEVEL, LOG_FORMAT
from src.db import get_connection
from src.calc.dso import METHOD_RATIO
from src.transform.due_dates import DEFAULT_DUE_DAYS

logger = logging.getLogger(__name__)

INPUT_TABLE = "so_open_with_expected_invoice_date"
OUTPUT_TABLE = "so_open_with_expected_collection"

SOURCE_STREAM = "open_so"

# so_timing_method tier labels, recording which lag drove each line.
SO_METHOD_DSO_RATIO = "so_dso_ratio"   # Tier 1: empirical DSO from open AR
SO_METHOD_TERMS = "so_terms"           # Tier 2: customer card net terms
SO_METHOD_DEFAULT = "so_default"       # Tier 3: customer absent -> house NET30

# Passthrough label for the underlying DSO classification when a customer is
# absent from bc_customer_dso (kept on the existing timing_method column).
METHOD_MASTER_FALLBACK = "master_fallback"


def stamp_expected_collection_dates(
    so_df: pd.DataFrame,
    dso_df: pd.DataFrame,
    as_of_date: dt.date,
) -> pd.DataFrame:
    """Resolve a per-line collection lag via the 3-tier waterfall, then stamp dates.

    Left-joins bc_customer_dso (dso_days, dso_method, terms_days) onto each SO
    line and selects the lag tier (see module docstring). raw_collection_date =
    plannedShipmentDate + lag; overdue rows clamp to as_of_date and are tagged.
    Adds so_timing_method (the tier) and keeps source_stream = "open_so".
    """
    df = so_df.merge(
        dso_df[["customerNumber", "dso_days", "dso_method", "terms_days"]],
        on="customerNumber",
        how="left",
    )

    present = df["dso_method"].notna()
    is_ratio = present & (df["dso_method"] == METHOD_RATIO)   # Tier 1
    is_terms = present & ~is_ratio                            # Tier 2

    dso_days = pd.to_numeric(df["dso_days"], errors="coerce")
    terms_days = pd.to_numeric(df["terms_days"], errors="coerce")

    # Effective collection lag: default -> overlaid with card terms (tier 2) ->
    # overlaid with empirical DSO (tier 1). Tiers 1 and 2 are mutually exclusive.
    lag = pd.Series(float(DEFAULT_DUE_DAYS), index=df.index)
    lag = lag.mask(is_terms, terms_days)
    lag = lag.mask(is_ratio, dso_days)
    df["dso_days_effective"] = lag.fillna(DEFAULT_DUE_DAYS).astype(int)

    so_method = pd.Series(SO_METHOD_DEFAULT, index=df.index)
    so_method = so_method.mask(is_terms, SO_METHOD_TERMS)
    so_method = so_method.mask(is_ratio, SO_METHOD_DSO_RATIO)
    df["so_timing_method"] = so_method

    # Existing column kept for shape: the customer's DSO classification passthrough
    # (master_fallback when the customer is absent from bc_customer_dso).
    df["timing_method"] = df["dso_method"].fillna(METHOD_MASTER_FALLBACK)
    df = df.drop(columns=["dso_days", "dso_method", "terms_days"])

    # raw_collection_date = plannedShipmentDate + lag; clamp to as_of for overdue.
    base = pd.to_datetime(df["plannedShipmentDate"])
    raw_collection = base + pd.to_timedelta(df["dso_days_effective"], unit="D")
    as_of_ts = pd.Timestamp(as_of_date)

    df["was_overdue"] = raw_collection < as_of_ts
    df["days_overdue"] = (as_of_ts - raw_collection).dt.days.clip(lower=0).astype(int)
    df["expected_collection_date"] = raw_collection.clip(lower=as_of_ts).dt.date
    df["plannedShipmentDate"] = base.dt.date
    df["expected_invoice_date"] = pd.to_datetime(df["expected_invoice_date"]).dt.date
    df["source_stream"] = SOURCE_STREAM

    return df


def load_table(name: str) -> pd.DataFrame:
    """Read an entire SQLite table into a DataFrame."""
    with get_connection() as conn:
        return pd.read_sql(f"SELECT * FROM {name}", conn)


def write_to_sqlite(df: pd.DataFrame, table_name: str = OUTPUT_TABLE) -> int:
    """Replace the table contents with df. Returns the number of rows written."""
    with get_connection() as conn:
        df.to_sql(table_name, conn, if_exists="replace", index=False)
    return len(df)


def _summary(df: pd.DataFrame, as_of: dt.date) -> dict:
    overdue = df[df["was_overdue"]]
    future = df[~df["was_overdue"]]
    return {
        "rows": int(len(df)),
        "as_of": as_of.isoformat(),
        "overdue_rows": int(len(overdue)),
        "overdue_dollars": round(float(overdue["outstandingAmount"].sum()), 2),
        "future_rows": int(len(future)),
        "future_dollars": round(float(future["outstandingAmount"].sum()), 2),
        "so_timing_methods": df["so_timing_method"].value_counts().to_dict(),
    }


def run(as_of_date: Optional[dt.date] = None) -> None:
    """Entrypoint: load open SO lines + DSO, stamp expected_collection_date, write."""
    logging.basicConfig(level=LOG_LEVEL, format=LOG_FORMAT)
    if as_of_date is None:
        as_of_date = dt.date.today()
    so_df = load_table(INPUT_TABLE)
    dso_df = load_table("bc_customer_dso")
    logger.info(
        "Loaded %d open SO lines, %d DSO rows; as_of=%s",
        len(so_df), len(dso_df), as_of_date.isoformat(),
    )

    stamped = stamp_expected_collection_dates(so_df, dso_df, as_of_date)
    logger.info("SO stamping summary: %s", _summary(stamped, as_of_date))

    n = write_to_sqlite(stamped)
    logger.info("Wrote %d rows to SQLite table %s", n, OUTPUT_TABLE)


if __name__ == "__main__":
    run()
