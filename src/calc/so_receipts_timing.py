"""Stamp expected_collection_date on each open SO line from per-customer DSO.

The open-sales-order analogue of src.calc.receipts_timing. For each open SO line:
    raw_collection_date      = expected_invoice_date + customer's dso_days
    expected_collection_date = max(raw_collection_date, as_of_date)   (overdue clamp)

It mirrors receipts_timing exactly -- same DSO join, same overdue clamp, same
was_overdue / days_overdue tagging, same timing_method passthrough -- with only
these differences:
  - the input date column is expected_invoice_date (not postingDate); an SO line
    becomes a receivable when it ships/invoices, then collects after the DSO lag;
  - there is no documentType filter (SO lines are already Item-only);
  - a source_stream = "open_so" column tags every row so the bucketing/combined
    layer can keep open-AR and open-SO contributions distinct;
  - the value carried forward is outstandingAmount (not "amount").

DSO-missing fallback: a customer present on an SO line but absent from
bc_customer_dso falls back to DEFAULT_DUE_DAYS (the same NET30 default the AR
side's due-date transform uses), tagged timing_method = "master_fallback". SO
lines carry no per-row dueDays, so the module-level default stands in for it.

Output table: so_open_with_expected_collection. The bucketing layer maps
expected_collection_date to forecast week 1-13 and aggregates outstandingAmount.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Optional

import pandas as pd

from src.config import LOG_LEVEL, LOG_FORMAT
from src.db import get_connection
from src.transform.due_dates import DEFAULT_DUE_DAYS

logger = logging.getLogger(__name__)

INPUT_TABLE = "so_open_with_expected_invoice_date"
OUTPUT_TABLE = "so_open_with_expected_collection"

SOURCE_STREAM = "open_so"

# Applied when a customer appears on an SO line but isn't present in
# bc_customer_dso. SO lines have no per-row dueDays, so we fall back to the
# project default due days (NET30), mirroring receipts_timing's master fallback.
METHOD_MASTER_FALLBACK = "master_fallback"


def stamp_expected_collection_dates(
    so_df: pd.DataFrame,
    dso_df: pd.DataFrame,
    as_of_date: dt.date,
) -> pd.DataFrame:
    """Stamp expected_collection_date, was_overdue, days_overdue on each SO line.

    Joins per-customer DSO (dso_days + dso_method) onto each line and computes
    raw_collection_date = expected_invoice_date + dso_days. Overdue rows
    (raw_collection_date < as_of_date) clamp to as_of_date and are tagged.
    Customers missing from dso_df fall back to DEFAULT_DUE_DAYS with
    timing_method = "master_fallback".
    """
    df = so_df.merge(
        dso_df[["customerNumber", "dso_days", "dso_method"]],
        on="customerNumber",
        how="left",
    )

    # Effective DSO: empirical (or terms-based) DSO from the lookup if present,
    # otherwise the project default due days (SO lines carry no per-row dueDays).
    df["dso_days_effective"] = df["dso_days"].fillna(DEFAULT_DUE_DAYS).astype(int)
    df["timing_method"] = df["dso_method"].fillna(METHOD_MASTER_FALLBACK)
    df = df.drop(columns=["dso_days", "dso_method"])

    invoice = pd.to_datetime(df["expected_invoice_date"])
    raw_collection = invoice + pd.to_timedelta(df["dso_days_effective"], unit="D")
    as_of_ts = pd.Timestamp(as_of_date)

    df["was_overdue"] = raw_collection < as_of_ts
    df["days_overdue"] = (as_of_ts - raw_collection).dt.days.clip(lower=0).astype(int)
    df["expected_collection_date"] = raw_collection.clip(lower=as_of_ts).dt.date
    df["expected_invoice_date"] = invoice.dt.date
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
        "timing_methods": df["timing_method"].value_counts().to_dict(),
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
