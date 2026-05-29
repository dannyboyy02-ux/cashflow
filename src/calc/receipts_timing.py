"""Stamp expected_collection_date on each open AR row from per-customer DSO.

For each open AR entry:
    raw_collection_date = postingDate + customer's dso_days
    expected_collection_date = max(raw_collection_date, as_of_date)

The clamp to as_of_date treats overdue AR as expected imminently (within the
current forecast week). Each row is tagged with was_overdue and days_overdue
so downstream consumers can split "natural-timing" cash from "swept-from-
overdue" cash for variance and lender reporting.

The dso_method from bc_customer_dso flows through as timing_method, preserving
visibility into whether the timing came from an empirical ratio, a terms-based
fallback, or a master-fallback (customer in AR but missing from the DSO table,
which should be rare).

Output table: ar_open_with_expected_collection. The bucketing layer (next
module) maps expected_collection_date to forecast week 1-13 and aggregates
amount by week.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Optional

import pandas as pd

from src.config import LOG_LEVEL, LOG_FORMAT
from src.db import get_connection

logger = logging.getLogger(__name__)

OUTPUT_TABLE = "ar_open_with_expected_collection"

# Method label applied when a customer appears on an AR row but isn't present
# in bc_customer_dso (data-quality miss; the master should cover every customer
# with open AR). In that case we fall back to the row's existing dueDays from
# the due-dates transform.
METHOD_MASTER_FALLBACK = "master_fallback"


def stamp_expected_collection_dates(
    ar: pd.DataFrame,
    dso_df: pd.DataFrame,
    as_of_date: dt.date,
) -> pd.DataFrame:
    """Stamp expected_collection_date, was_overdue, days_overdue on each AR row.

    Joins the per-customer DSO (dso_days + dso_method) onto each AR row and
    computes raw_collection_date = postingDate + dso_days. Overdue rows
    (raw_collection_date < as_of_date) are clamped to as_of_date and tagged.

    Customers in AR but missing from dso_df (rare) fall back to the row's
    dueDays, with timing_method = "master_fallback". The clamp logic still
    applies; only the source of the day count differs.
    """
    df = ar.merge(
        dso_df[["customerNumber", "dso_days", "dso_method"]],
        on="customerNumber",
        how="left",
    )

    df["dso_days_effective"] = df["dso_days"].fillna(df["dueDays"]).astype(int)
    df["timing_method"] = df["dso_method"].fillna(METHOD_MASTER_FALLBACK)
    df = df.drop(columns=["dso_days", "dso_method"])

    posting = pd.to_datetime(df["postingDate"])
    raw_collection = posting + pd.to_timedelta(df["dso_days_effective"], unit="D")
    as_of_ts = pd.Timestamp(as_of_date)

    df["was_overdue"] = raw_collection < as_of_ts
    df["days_overdue"] = (as_of_ts - raw_collection).dt.days.clip(lower=0).astype(int)
    df["expected_collection_date"] = raw_collection.clip(lower=as_of_ts).dt.date
    df["postingDate"] = posting.dt.date

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
    """Diagnostic summary for the log line.

    Surfaces the overdue dollar volume separately from natural future-timed
    cash, since the overdue clamp is the one place the model exercises
    judgment. Worth seeing every run.
    """
    overdue = df[df["was_overdue"]]
    future = df[~df["was_overdue"]]
    return {
        "rows": int(len(df)),
        "as_of": as_of.isoformat(),
        "overdue_rows": int(len(overdue)),
        "overdue_dollars": round(float(overdue["amount"].sum()), 2),
        "future_rows": int(len(future)),
        "future_dollars": round(float(future["amount"].sum()), 2),
        "timing_methods": df["timing_method"].value_counts().to_dict(),
    }


def run(as_of_date: Optional[dt.date] = None) -> None:
    """Entrypoint: load tables, stamp expected_collection_date, write result."""
    logging.basicConfig(level=LOG_LEVEL, format=LOG_FORMAT)
    if as_of_date is None:
        as_of_date = dt.date.today()
    ar = load_table("ar_open_with_due_dates")
    dso_df = load_table("bc_customer_dso")
    logger.info(
        "Loaded %d AR rows, %d DSO rows; as_of=%s",
        len(ar), len(dso_df), as_of_date.isoformat(),
    )

    stamped = stamp_expected_collection_dates(ar, dso_df, as_of_date)
    logger.info("Stamping summary: %s", _summary(stamped, as_of_date))

    n = write_to_sqlite(stamped)
    logger.info("Wrote %d rows to SQLite table %s", n, OUTPUT_TABLE)


if __name__ == "__main__":
    run()