"""Grade each closed week's forecast against what actually cleared the bank.

This is the discipline that earns a 13-week forecast its trust: not "how did my
forecast move since last week" (that's src.calc.variance, snapshot-to-snapshot),
but "was the forecast I made ENTERING a week right, once the week closed?"

For each closed week the user records actuals (see inputs/actuals.json), we pull
the forecast that was standing at the START of that week -- i.e. the snapshot
whose forecast_week == 1 lands on that week's Monday -- and compare:

    actuals (entered)                 forecast (snapshot, week 1 of that week)
    -----------------                 ----------------------------------------
    actual_receipts                   forecast_receipts      (AR + open-SO)
    actual_disbursements              forecast_disbursements (AP + open-PO)
    actual_net = recpt - disb         forecast_net

Output table forecast_vs_actual (one row per closed week with both a recorded
actual and a standing forecast). variance = actual - forecast.

SCOPE (v1): grades the snapshotted operating streams (receipts, disbursements).
Payroll, debt service, and the revolver are deterministic/derived and are not
yet snapshotted, so ending-cash accuracy is a documented follow-up (snapshot
cash_position_by_week, then grade ending cash here too).

INPUT (where actuals are entered): inputs/actuals.json (gitignored -- real bank
figures), keyed by the week-start Monday (YYYY-MM-DD):

    {"2026-06-01": {"receipts": 12345678.90, "disbursements": 9876543.21}, ...}

A future "Actuals" workbook input tab will write this same JSON so the CFO can
type the numbers in the workbook they already open.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Optional

import pandas as pd

from src.config import LOG_LEVEL, LOG_FORMAT, INPUTS_DIR
from src.db import get_connection
from src.calc.snapshot import AR_SNAPSHOT_TABLE, AP_SNAPSHOT_TABLE

logger = logging.getLogger(__name__)

INPUT_FILE = INPUTS_DIR / "actuals.json"
OUTPUT_TABLE = "forecast_vs_actual"

OUTPUT_COLUMNS = [
    "week_start_date", "forecast_snapshot_date",
    "forecast_receipts", "actual_receipts", "receipts_variance",
    "forecast_disbursements", "actual_disbursements", "disbursements_variance",
    "forecast_net", "actual_net", "net_variance",
]


def load_actuals(path: Optional[Path] = None) -> dict:
    """Read the recorded weekly actuals, or {} if the file is absent."""
    path = Path(path) if path is not None else INPUT_FILE
    if not path.exists():
        return {}
    with open(path) as f:
        return json.load(f) or {}


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    cur = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    )
    return cur.fetchone() is not None


def load_table(name: str) -> pd.DataFrame:
    """Read a table, or an empty frame if it doesn't exist yet."""
    with get_connection() as conn:
        if not _table_exists(conn, name):
            return pd.DataFrame()
        return pd.read_sql(f"SELECT * FROM {name}", conn)


def _forecast_week1(snap: pd.DataFrame, value_col: str, week_start: str) -> tuple[Optional[str], float]:
    """The week-1 forecast for `week_start`, taken from the snapshot made entering it.

    Among snapshot rows whose forecast_week == 1 land on this Monday, pick the
    earliest snapshot_date (the forecast standing as the week opened) and sum its
    value column. Returns (snapshot_date_used, total) or (None, 0.0) if none.
    """
    if snap.empty or "forecast_week" not in snap.columns:
        return None, 0.0
    wk1 = snap[(snap["forecast_week"] == 1) & (snap["week_start_date"] == week_start)]
    if wk1.empty:
        return None, 0.0
    used = min(wk1["snapshot_date"].unique())
    total = float(wk1[wk1["snapshot_date"] == used][value_col].sum())
    return used, total


def compute_forecast_vs_actual(
    ar_snap: pd.DataFrame,
    ap_snap: pd.DataFrame,
    actuals: dict,
) -> pd.DataFrame:
    """Compare each closed week's actuals to the forecast made entering that week.

    Only weeks that have BOTH a recorded actual and a standing week-1 forecast
    are graded; others are skipped (can't compare). variance = actual - forecast.
    """
    rows = []
    for week_start in sorted(actuals.keys()):
        a = actuals[week_start] or {}
        snap_r, fc_receipts = _forecast_week1(ar_snap, "receipts", week_start)
        snap_d, fc_disb = _forecast_week1(ap_snap, "disbursements", week_start)
        if snap_r is None and snap_d is None:
            continue  # no forecast was standing for this week -> cannot grade
        actual_receipts = float(a.get("receipts", 0.0))
        actual_disb = float(a.get("disbursements", 0.0))
        fc_net = fc_receipts - fc_disb
        actual_net = actual_receipts - actual_disb
        rows.append({
            "week_start_date": week_start,
            "forecast_snapshot_date": snap_r or snap_d,
            "forecast_receipts": round(fc_receipts, 2),
            "actual_receipts": round(actual_receipts, 2),
            "receipts_variance": round(actual_receipts - fc_receipts, 2),
            "forecast_disbursements": round(fc_disb, 2),
            "actual_disbursements": round(actual_disb, 2),
            "disbursements_variance": round(actual_disb - fc_disb, 2),
            "forecast_net": round(fc_net, 2),
            "actual_net": round(actual_net, 2),
            "net_variance": round(actual_net - fc_net, 2),
        })
    return pd.DataFrame(rows, columns=OUTPUT_COLUMNS)


def write_to_sqlite(df: pd.DataFrame, table_name: str = OUTPUT_TABLE) -> int:
    """Replace the table contents with df. Returns the number of rows written."""
    with get_connection() as conn:
        df.to_sql(table_name, conn, if_exists="replace", index=False)
    return len(df)


def run() -> None:
    """Entrypoint: grade recorded actuals against the standing forecasts, write."""
    logging.basicConfig(level=LOG_LEVEL, format=LOG_FORMAT)
    actuals = load_actuals()
    ar_snap = load_table(AR_SNAPSHOT_TABLE)
    ap_snap = load_table(AP_SNAPSHOT_TABLE)
    result = compute_forecast_vs_actual(ar_snap, ap_snap, actuals)
    n = write_to_sqlite(result)
    if result.empty:
        logger.info(
            "No gradeable weeks yet (need a recorded actual in inputs/actuals.json "
            "AND a standing week-1 snapshot for that week); wrote empty %s.",
            OUTPUT_TABLE,
        )
    else:
        logger.info(
            "Graded %d closed week(s); net variance sum=%.2f -> %s",
            n, round(float(result["net_variance"].sum()), 2), OUTPUT_TABLE,
        )


if __name__ == "__main__":
    run()
