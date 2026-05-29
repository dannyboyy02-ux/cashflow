"""Bucket expected_collection_date into 13 calendar-week forecast columns.

Convention:
  - Calendar weeks, Monday-anchored (Mon=0..Sun=6).
  - Week 1 = the week containing as_of_date. For a Friday refresh, week 1
    spans the preceding Mon through Sun, and the overdue-clamped rows
    (expected_collection_date == as_of_date) land in week 1 alongside
    whatever else collects this calendar week.
  - Week 13 covers days as_of_week_monday + 84 through + 90.
  - Anything dated beyond week 13's Sunday is "out of horizon" and excluded
    from the aggregated output. The severely-aged customer's $44k from the
    receipts-timing run is the canonical example.

The output is long-format (customerNumber x forecast_week x receipts), one
row per non-zero (customer, week) combination. Wide-pivot views (13 columns
across) are an Excel-writer concern, not a calc concern -- trivial to derive
from this table via SQL or pandas pivot.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Optional

import pandas as pd

from src.config import LOG_LEVEL, LOG_FORMAT, FORECAST_HORIZON_WEEKS
from src.db import get_connection

logger = logging.getLogger(__name__)

OUTPUT_TABLE = "ar_receipts_by_week"


def monday_of_week(d: dt.date) -> dt.date:
    """Return the Monday of the calendar week containing d."""
    return d - dt.timedelta(days=d.weekday())


def assign_forecast_week(
    stamped: pd.DataFrame,
    as_of_date: dt.date,
) -> pd.DataFrame:
    """Add forecast_week (1-indexed) and week_start_date columns.

    Adds two columns to a stamped AR DataFrame:
      forecast_week:   1-indexed week number (1 = week containing as_of_date).
                       Rows beyond the horizon get values > horizon_weeks.
                       Rows clamped to as_of always land in week 1.
      week_start_date: the Monday that begins forecast_week's calendar week.
    """
    week_1_monday = monday_of_week(as_of_date)
    df = stamped.copy()

    coll_dates = pd.to_datetime(df["expected_collection_date"])
    week_1_ts = pd.Timestamp(week_1_monday)

    # Integer floor-division of day-difference yields the 0-indexed week offset.
    # Because expected_collection_date is always >= as_of_date >= week_1_monday,
    # the offset is always >= 0; no negative-floor-div weirdness.
    offset_0idx = ((coll_dates - week_1_ts).dt.days // 7).astype(int)

    df["forecast_week"] = offset_0idx + 1
    df["week_start_date"] = (
        week_1_ts + pd.to_timedelta(offset_0idx * 7, unit="D")
    ).dt.date

    return df


def aggregate_receipts_by_week(
    stamped_with_weeks: pd.DataFrame,
    horizon_weeks: int = FORECAST_HORIZON_WEEKS,
) -> pd.DataFrame:
    """Group by (customer, forecast_week, week_start_date), summing amounts.

    Filters to in-horizon rows (forecast_week between 1 and horizon_weeks).
    Out-of-horizon rows are excluded from the aggregate; the run summary
    surfaces them as a separate count/dollar figure so they're not silently
    dropped.
    """
    in_horizon = stamped_with_weeks[
        (stamped_with_weeks["forecast_week"] >= 1)
        & (stamped_with_weeks["forecast_week"] <= horizon_weeks)
    ]
    agg = (
        in_horizon
        .groupby(
            ["customerNumber", "forecast_week", "week_start_date"],
            as_index=False,
        )["amount"]
        .sum()
    )
    agg = agg.rename(columns={"amount": "receipts"})
    return agg


def load_table(name: str) -> pd.DataFrame:
    """Read an entire SQLite table into a DataFrame."""
    with get_connection() as conn:
        return pd.read_sql(f"SELECT * FROM {name}", conn)


def write_to_sqlite(df: pd.DataFrame, table_name: str = OUTPUT_TABLE) -> int:
    """Replace the table contents with df. Returns the number of rows written."""
    with get_connection() as conn:
        df.to_sql(table_name, conn, if_exists="replace", index=False)
    return len(df)


def _summary(
    agg: pd.DataFrame,
    stamped_with_weeks: pd.DataFrame,
    horizon_weeks: int,
    as_of_date: dt.date,
) -> dict:
    """Diagnostic summary for the log line."""
    in_horizon = stamped_with_weeks[
        (stamped_with_weeks["forecast_week"] >= 1)
        & (stamped_with_weeks["forecast_week"] <= horizon_weeks)
    ]
    beyond = stamped_with_weeks[stamped_with_weeks["forecast_week"] > horizon_weeks]
    return {
        "horizon_weeks": horizon_weeks,
        "as_of": as_of_date.isoformat(),
        "week_1_monday": monday_of_week(as_of_date).isoformat(),
        "in_horizon_rows": int(len(in_horizon)),
        "in_horizon_dollars": round(float(in_horizon["amount"].sum()), 2),
        "beyond_horizon_rows": int(len(beyond)),
        "beyond_horizon_dollars": round(float(beyond["amount"].sum()), 2),
        "aggregated_rows": int(len(agg)),
    }


def run(
    as_of_date: Optional[dt.date] = None,
    horizon_weeks: int = FORECAST_HORIZON_WEEKS,
) -> None:
    """Entrypoint: load stamped AR, assign weeks, aggregate, write result."""
    logging.basicConfig(level=LOG_LEVEL, format=LOG_FORMAT)
    if as_of_date is None:
        as_of_date = dt.date.today()
    stamped = load_table("ar_open_with_expected_collection")
    logger.info(
        "Loaded %d stamped AR rows; as_of=%s, horizon=%d weeks",
        len(stamped), as_of_date.isoformat(), horizon_weeks,
    )

    stamped_with_weeks = assign_forecast_week(stamped, as_of_date)
    agg = aggregate_receipts_by_week(stamped_with_weeks, horizon_weeks)
    logger.info("Bucketing summary: %s", _summary(agg, stamped_with_weeks, horizon_weeks, as_of_date))

    n = write_to_sqlite(agg)
    logger.info("Wrote %d rows to SQLite table %s", n, OUTPUT_TABLE)


if __name__ == "__main__":
    run()