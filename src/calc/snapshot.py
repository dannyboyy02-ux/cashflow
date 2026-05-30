"""Append a dated snapshot of the current week-bucketed forecast.

Each pipeline run stamps the current ar_receipts_by_week / ap_disbursements_by_week
tables with a snapshot_date and as_of_date and appends them to history tables, so
the variance layer (src.calc.variance) can compute day-over-day change.

  ar_receipts_by_week        --stamp + append-->  ar_receipts_snapshots
  ap_disbursements_by_week   --stamp + append-->  ap_disbursements_snapshots

APPEND-ONLY: every run adds a new dated batch; prior snapshots are preserved.
The one exception is a same-day re-run: rows whose (snapshot_date, as_of_date)
match the current run are deleted first, then re-inserted, so re-running the
pipeline twice in one day overwrites cleanly rather than doubling the history.

snapshot_date is the calendar date this script runs. as_of_date is the forecast
as-of date.

NOTE (design decision): the bucketed source tables do NOT carry an as_of_date
column -- the bucketing layer only stores week_start_date, from which the exact
as-of date is not recoverable. as_of_date is therefore taken as a run() parameter
defaulting to today, mirroring receipts_timing.run() / bucketing.run(). When this
module is invoked at the tail of the pipeline (same day bucketing ran), the
default is correct.
"""
from __future__ import annotations

import datetime as dt
import logging
import sqlite3
from typing import Optional

import pandas as pd

from src.config import LOG_LEVEL, LOG_FORMAT
from src.db import get_connection

logger = logging.getLogger(__name__)

AR_SOURCE_TABLE = "ar_receipts_by_week"
AP_SOURCE_TABLE = "ap_disbursements_by_week"

AR_SNAPSHOT_TABLE = "ar_receipts_snapshots"
AP_SNAPSHOT_TABLE = "ap_disbursements_snapshots"

# Column order the snapshot tables present: the two date stamps first, then the
# source columns verbatim.
AR_SNAPSHOT_COLUMNS = [
    "snapshot_date", "as_of_date", "customerNumber",
    "forecast_week", "week_start_date", "receipts",
]
AP_SNAPSHOT_COLUMNS = [
    "snapshot_date", "as_of_date", "vendorNumber",
    "forecast_week", "week_start_date", "disbursements",
]


def load_table(name: str) -> pd.DataFrame:
    """Read an entire SQLite table into a DataFrame."""
    with get_connection() as conn:
        return pd.read_sql(f"SELECT * FROM {name}", conn)


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    cur = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    )
    return cur.fetchone() is not None


def stamp_snapshot(
    source: pd.DataFrame,
    snapshot_date: dt.date,
    as_of_date: dt.date,
    columns: list[str],
) -> pd.DataFrame:
    """Prepend snapshot_date + as_of_date (ISO strings) and order the columns.

    Works on an empty source frame too (produces a 0-row frame with the right
    columns), so a run over empty forecasts still creates/clears the table.
    """
    df = source.copy()
    df["snapshot_date"] = snapshot_date.isoformat()
    df["as_of_date"] = as_of_date.isoformat()
    return df[columns]


def append_snapshot(
    df: pd.DataFrame,
    table_name: str,
    snapshot_date: dt.date,
    as_of_date: dt.date,
) -> int:
    """Append df to table_name, first clearing any rows for this run's date pair.

    The delete-then-append makes a same-day re-run idempotent without disturbing
    snapshots from other dates. Returns the number of rows written.
    """
    conn = get_connection()
    try:
        if _table_exists(conn, table_name):
            conn.execute(
                f"DELETE FROM {table_name} WHERE snapshot_date = ? AND as_of_date = ?",
                (snapshot_date.isoformat(), as_of_date.isoformat()),
            )
        df.to_sql(table_name, conn, if_exists="append", index=False)
        conn.commit()
    finally:
        conn.close()
    return len(df)


def run(
    snapshot_date: Optional[dt.date] = None,
    as_of_date: Optional[dt.date] = None,
) -> None:
    """Entrypoint: stamp and append today's forecast into the snapshot history."""
    logging.basicConfig(level=LOG_LEVEL, format=LOG_FORMAT)
    if snapshot_date is None:
        snapshot_date = dt.date.today()
    if as_of_date is None:
        as_of_date = dt.date.today()

    ar = load_table(AR_SOURCE_TABLE)
    ap = load_table(AP_SOURCE_TABLE)
    logger.info(
        "Loaded %d AR receipt rows, %d AP disbursement rows to snapshot",
        len(ar), len(ap),
    )

    ar_snap = stamp_snapshot(ar, snapshot_date, as_of_date, AR_SNAPSHOT_COLUMNS)
    ap_snap = stamp_snapshot(ap, snapshot_date, as_of_date, AP_SNAPSHOT_COLUMNS)

    n_ar = append_snapshot(ar_snap, AR_SNAPSHOT_TABLE, snapshot_date, as_of_date)
    n_ap = append_snapshot(ap_snap, AP_SNAPSHOT_TABLE, snapshot_date, as_of_date)

    logger.info(
        "Wrote %d AR snapshot rows, %d AP snapshot rows for snapshot_date=%s, as_of=%s",
        n_ar, n_ap, snapshot_date.isoformat(), as_of_date.isoformat(),
    )


if __name__ == "__main__":
    run()
