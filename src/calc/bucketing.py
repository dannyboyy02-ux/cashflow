"""Bucket expected cash dates into 13 calendar-week forecast columns.

Handles BOTH sides of the forecast on the same Monday-anchored grid:
  - AR receipts   (ar_open_with_expected_collection      -> ar_receipts_by_week)
  - AP disbursements (ap_disbursements_with_expected_payment_date
                                                          -> ap_disbursements_by_week)

The week-assignment and aggregation logic is identical for both sides -- only
the input table, the date column, the entity key, and the value column differ.
So the core lives in two generic functions (assign_forecast_week with a
date_col parameter, and aggregate_by_week) and each side gets a thin wrapper.

Convention:
  - Calendar weeks, Monday-anchored (Mon=0..Sun=6).
  - Week 1 = the week containing as_of_date. For a Friday refresh, week 1
    spans the preceding Mon through Sun, and the overdue-clamped rows
    (expected date == as_of_date) land in week 1 alongside whatever else
    falls in this calendar week.
  - Week 13 covers days as_of_week_monday + 84 through + 90.
  - Anything dated beyond week 13's Sunday is "out of horizon" and excluded
    from the aggregated output. The severely-aged customer's $44k from the
    receipts-timing run is the canonical example.

The output is long-format (entity x forecast_week x value), one row per
non-zero (entity, week) combination. Wide-pivot views (13 columns across) are
an Excel-writer concern, not a calc concern -- trivial to derive from this
table via SQL or pandas pivot.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Optional

import pandas as pd

from src.config import LOG_LEVEL, LOG_FORMAT, FORECAST_HORIZON_WEEKS
from src.db import get_connection

logger = logging.getLogger(__name__)

# AR side: stamped open AR with expected_collection_date -> receipts by week.
AR_INPUT_TABLE = "ar_open_with_expected_collection"
AR_OUTPUT_TABLE = "ar_receipts_by_week"
AR_DATE_COL = "expected_collection_date"
AR_ENTITY_COL = "customerNumber"
AR_VALUE_COL = "amount"
AR_AGG_COL = "receipts"

# AP side: stamped disbursements with expected_payment_date -> disbursements by week.
AP_INPUT_TABLE = "ap_disbursements_with_expected_payment_date"
AP_OUTPUT_TABLE = "ap_disbursements_by_week"
AP_DATE_COL = "expected_payment_date"
AP_ENTITY_COL = "vendorNumber"
AP_VALUE_COL = "disbursement_amount"
AP_AGG_COL = "disbursements"

# Backwards-compatible alias: the original single-purpose constant name.
OUTPUT_TABLE = AR_OUTPUT_TABLE


def monday_of_week(d: dt.date) -> dt.date:
    """Return the Monday of the calendar week containing d."""
    return d - dt.timedelta(days=d.weekday())


def assign_forecast_week(
    stamped: pd.DataFrame,
    as_of_date: dt.date,
    date_col: str = AR_DATE_COL,
) -> pd.DataFrame:
    """Add forecast_week (1-indexed) and week_start_date columns.

    Generic over the date column so both AR (expected_collection_date) and AP
    (expected_payment_date) can share it. The default preserves the original
    AR-only call signature.

    Adds two columns:
      forecast_week:   1-indexed week number (1 = week containing as_of_date).
                       Rows beyond the horizon get values > horizon_weeks.
                       Overdue-clamped rows always land in week 1.
      week_start_date: the Monday that begins forecast_week's calendar week.
    """
    week_1_monday = monday_of_week(as_of_date)
    df = stamped.copy()

    cash_dates = pd.to_datetime(df[date_col])
    week_1_ts = pd.Timestamp(week_1_monday)

    # Integer floor-division of day-difference yields the 0-indexed week offset.
    # Because the expected date is always >= as_of_date >= week_1_monday, the
    # offset is always >= 0; no negative-floor-div weirdness.
    offset_0idx = ((cash_dates - week_1_ts).dt.days // 7).astype(int)

    df["forecast_week"] = offset_0idx + 1
    df["week_start_date"] = (
        week_1_ts + pd.to_timedelta(offset_0idx * 7, unit="D")
    ).dt.date

    return df


def aggregate_by_week(
    stamped_with_weeks: pd.DataFrame,
    entity_col: str,
    value_col: str,
    agg_col: str,
    horizon_weeks: int = FORECAST_HORIZON_WEEKS,
) -> pd.DataFrame:
    """Group by (entity, forecast_week, week_start_date), summing value_col.

    Filters to in-horizon rows (forecast_week between 1 and horizon_weeks).
    Out-of-horizon rows are excluded; the run summary surfaces them as a
    separate count/dollar figure so they're not silently dropped. The summed
    column is renamed to agg_col in the output.
    """
    in_horizon = stamped_with_weeks[
        (stamped_with_weeks["forecast_week"] >= 1)
        & (stamped_with_weeks["forecast_week"] <= horizon_weeks)
    ]
    agg = (
        in_horizon
        .groupby(
            [entity_col, "forecast_week", "week_start_date"],
            as_index=False,
        )[value_col]
        .sum()
    )
    agg = agg.rename(columns={value_col: agg_col})
    return agg


def aggregate_receipts_by_week(
    stamped_with_weeks: pd.DataFrame,
    horizon_weeks: int = FORECAST_HORIZON_WEEKS,
) -> pd.DataFrame:
    """AR wrapper: receipts per (customer, week). See aggregate_by_week."""
    return aggregate_by_week(
        stamped_with_weeks,
        entity_col=AR_ENTITY_COL,
        value_col=AR_VALUE_COL,
        agg_col=AR_AGG_COL,
        horizon_weeks=horizon_weeks,
    )


def aggregate_disbursements_by_week(
    stamped_with_weeks: pd.DataFrame,
    horizon_weeks: int = FORECAST_HORIZON_WEEKS,
) -> pd.DataFrame:
    """AP wrapper: disbursements per (vendor, week). See aggregate_by_week.

    Sums disbursement_amount, which payments_timing.py already emits as a
    positive number on both streams, so the aggregate is directly comparable
    to AR receipts.
    """
    return aggregate_by_week(
        stamped_with_weeks,
        entity_col=AP_ENTITY_COL,
        value_col=AP_VALUE_COL,
        agg_col=AP_AGG_COL,
        horizon_weeks=horizon_weeks,
    )


def load_table(name: str) -> pd.DataFrame:
    """Read an entire SQLite table into a DataFrame."""
    with get_connection() as conn:
        return pd.read_sql(f"SELECT * FROM {name}", conn)


def write_to_sqlite(df: pd.DataFrame, table_name: str = AR_OUTPUT_TABLE) -> int:
    """Replace the table contents with df. Returns the number of rows written."""
    with get_connection() as conn:
        df.to_sql(table_name, conn, if_exists="replace", index=False)
    return len(df)


def _summary(
    agg: pd.DataFrame,
    stamped_with_weeks: pd.DataFrame,
    horizon_weeks: int,
    as_of_date: dt.date,
    value_col: str,
) -> dict:
    """Diagnostic summary for the log line, generic over the value column."""
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
        "in_horizon_dollars": round(float(in_horizon[value_col].sum()), 2),
        "beyond_horizon_rows": int(len(beyond)),
        "beyond_horizon_dollars": round(float(beyond[value_col].sum()), 2),
        "aggregated_rows": int(len(agg)),
    }


def run_receipts(
    as_of_date: Optional[dt.date] = None,
    horizon_weeks: int = FORECAST_HORIZON_WEEKS,
) -> None:
    """Bucket AR receipts: load stamped AR, assign weeks, aggregate, write."""
    if as_of_date is None:
        as_of_date = dt.date.today()
    stamped = load_table(AR_INPUT_TABLE)
    logger.info(
        "Loaded %d stamped AR rows; as_of=%s, horizon=%d weeks",
        len(stamped), as_of_date.isoformat(), horizon_weeks,
    )

    stamped_with_weeks = assign_forecast_week(stamped, as_of_date, date_col=AR_DATE_COL)
    agg = aggregate_receipts_by_week(stamped_with_weeks, horizon_weeks)
    logger.info(
        "AR receipts bucketing summary: %s",
        _summary(agg, stamped_with_weeks, horizon_weeks, as_of_date, AR_VALUE_COL),
    )

    n = write_to_sqlite(agg, AR_OUTPUT_TABLE)
    logger.info("Wrote %d rows to SQLite table %s", n, AR_OUTPUT_TABLE)


def run_disbursements(
    as_of_date: Optional[dt.date] = None,
    horizon_weeks: int = FORECAST_HORIZON_WEEKS,
) -> None:
    """Bucket AP disbursements: load stamped AP, assign weeks, aggregate, write."""
    if as_of_date is None:
        as_of_date = dt.date.today()
    stamped = load_table(AP_INPUT_TABLE)
    logger.info(
        "Loaded %d stamped AP rows; as_of=%s, horizon=%d weeks",
        len(stamped), as_of_date.isoformat(), horizon_weeks,
    )

    stamped_with_weeks = assign_forecast_week(stamped, as_of_date, date_col=AP_DATE_COL)
    agg = aggregate_disbursements_by_week(stamped_with_weeks, horizon_weeks)
    logger.info(
        "AP disbursements bucketing summary: %s",
        _summary(agg, stamped_with_weeks, horizon_weeks, as_of_date, AP_VALUE_COL),
    )

    n = write_to_sqlite(agg, AP_OUTPUT_TABLE)
    logger.info("Wrote %d rows to SQLite table %s", n, AP_OUTPUT_TABLE)


def run(
    as_of_date: Optional[dt.date] = None,
    horizon_weeks: int = FORECAST_HORIZON_WEEKS,
) -> None:
    """Entrypoint: bucket both AR receipts and AP disbursements in sequence."""
    logging.basicConfig(level=LOG_LEVEL, format=LOG_FORMAT)
    run_receipts(as_of_date, horizon_weeks)
    run_disbursements(as_of_date, horizon_weeks)


if __name__ == "__main__":
    run()
