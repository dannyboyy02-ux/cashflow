"""Revolver plug + consolidated weekly cash position (the forecast capstone).

Every cash stream built so far converges here. The revolver is a CALCULATED PLUG
that runs sequentially through the 13-week horizon: each week it draws when the
pre-revolver ending cash would fall below a minimum target, or repays from excess
cash when above target with a balance outstanding.

UNLIKE every other calc, this module does NOT fit the bucketed-aggregation
pattern -- week N's beginning cash and revolver balance depend on week N-1's
ending values, so it loops weeks 1..13 in order, carrying state forward.

It reads the already-bucketed weekly streams from SQLite:
    combined_receipts_by_week        (inflows)
    combined_disbursements_by_week   \\
    payroll_by_week.total_payroll     >  base outflows (pre-revolver)
    debt_principal_by_week.principal  /
    debt_interest_by_week.interest   /

Per week (target = minimum_cash_target, max_capacity = facility_total - lc_carve_out):
    revolver_interest    = round(begin_revolver * (sofr+spread)/52, 2)  # on opening balance
    total_outflows       = base_outflows + revolver_interest
    pre_revolver_ending  = begin_cash + inflows - total_outflows
    if pre < target:   draw min(target - pre, available_to_draw); breach if clamped
    elif pre > target and begin_revolver > 0:   repay min(pre - target, begin_revolver)
    else:              no change
    ending_cash      = pre_revolver_ending + revolver_change
    ending_revolver  = begin_revolver + revolver_change

Writes three tables: revolver_activity_by_week, revolver_interest_by_week (a
payroll-shaped disbursement stream for Phase 7d), and cash_position_by_week (the
consolidated weekly position Phase 7d renders directly). Mirrors the payroll /
debt_service input-file shell; graceful on a missing config. Does NOT touch the
Excel writer or snapshot.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import sqlite3
from pathlib import Path
from typing import Optional

import pandas as pd

from src.config import LOG_LEVEL, LOG_FORMAT, INPUTS_DIR, FORECAST_HORIZON_WEEKS
from src.db import get_connection
from src.calc.bucketing import monday_of_week

logger = logging.getLogger(__name__)

INPUT_FILE = INPUTS_DIR / "revolver_config.json"

ACTIVITY_TABLE = "revolver_activity_by_week"
INTEREST_TABLE = "revolver_interest_by_week"
CASH_POSITION_TABLE = "cash_position_by_week"
SOURCE_INTEREST = "revolver_interest"

ACTIVITY_COLUMNS = [
    "forecast_week", "week_start_date", "begin_revolver_balance",
    "revolver_draw", "revolver_repay", "ending_revolver_balance",
    "revolver_interest_accrued", "available_capacity", "capacity_breached",
]
INTEREST_COLUMNS = ["forecast_week", "week_start_date", "interest", "source_stream"]
CASH_POSITION_COLUMNS = [
    "forecast_week", "week_start_date", "beginning_cash", "total_inflows",
    "total_outflows", "pre_revolver_ending_cash", "revolver_draw",
    "revolver_repay", "ending_cash", "ending_revolver_balance",
]


def load_revolver_config(path: Optional[Path] = None) -> Optional[dict]:
    """Read the revolver config JSON, or return None if the file is absent."""
    path = Path(path) if path is not None else INPUT_FILE
    if not path.exists():
        return None
    with open(path) as f:
        return json.load(f)


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    cur = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    )
    return cur.fetchone() is not None


def _week_sums(conn: sqlite3.Connection, table: str, value_col: str) -> dict[int, float]:
    """{forecast_week -> SUM(value_col)} for a table, or {} if it doesn't exist."""
    if not _table_exists(conn, table):
        return {}
    rows = conn.execute(
        f"SELECT forecast_week, SUM({value_col}) FROM {table} GROUP BY forecast_week"
    ).fetchall()
    return {int(w): float(v or 0.0) for w, v in rows}


def compute_revolver(
    config: dict,
    inflows: dict[int, float],
    base_outflows: dict[int, float],
    as_of: dt.date,
    horizon_weeks: int = FORECAST_HORIZON_WEEKS,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Run the sequential revolver plug. Returns (activity, interest, cash_position).

    inflows / base_outflows are {forecast_week -> amount} (base_outflows excludes
    revolver interest, which is computed here on the opening balance). State is
    carried forward week to week; money columns are rounded to cents so the
    chain (wk N begin == wk N-1 ending) holds exactly.
    """
    facility_total = float(config.get("facility_total", 0.0))
    lc_carve_out = float(config.get("lc_carve_out", 0.0))
    target = float(config.get("minimum_cash_target", 0.0))
    weekly_rate = (float(config.get("sofr_rate", 0.0)) + float(config.get("spread", 0.0))) / 52
    max_capacity = facility_total - lc_carve_out

    begin_cash = float(config.get("beginning_cash", 0.0))
    begin_revolver = float(config.get("current_drawn_balance", 0.0))

    week_1_monday = monday_of_week(as_of)
    activity_rows, interest_rows, cash_rows = [], [], []

    for i in range(horizon_weeks):
        wk = i + 1
        monday = week_1_monday + dt.timedelta(days=7 * i)
        inflow = float(inflows.get(wk, 0.0))
        base_out = float(base_outflows.get(wk, 0.0))

        revolver_interest = round(begin_revolver * weekly_rate, 2)
        total_outflows = round(base_out + revolver_interest, 2)
        pre_revolver_ending = round(begin_cash + inflow - total_outflows, 2)

        available_to_draw = max(0.0, max_capacity - begin_revolver)
        draw = repay = 0.0
        capacity_breached = False

        if pre_revolver_ending < target:
            draw_needed = target - pre_revolver_ending
            draw = min(draw_needed, available_to_draw)
            capacity_breached = draw < draw_needed
            revolver_change = draw
        elif pre_revolver_ending > target and begin_revolver > 0:
            excess = pre_revolver_ending - target
            repay = min(excess, begin_revolver)
            revolver_change = -repay
        else:
            revolver_change = 0.0

        ending_revolver = round(begin_revolver + revolver_change, 2)
        ending_cash = round(pre_revolver_ending + revolver_change, 2)
        available_capacity = round(max_capacity - ending_revolver, 2)
        draw = round(draw, 2)
        repay = round(repay, 2)

        activity_rows.append({
            "forecast_week": wk, "week_start_date": monday,
            "begin_revolver_balance": round(begin_revolver, 2),
            "revolver_draw": draw, "revolver_repay": repay,
            "ending_revolver_balance": ending_revolver,
            "revolver_interest_accrued": revolver_interest,
            "available_capacity": available_capacity,
            "capacity_breached": bool(capacity_breached),
        })
        interest_rows.append({
            "forecast_week": wk, "week_start_date": monday,
            "interest": revolver_interest, "source_stream": SOURCE_INTEREST,
        })
        cash_rows.append({
            "forecast_week": wk, "week_start_date": monday,
            "beginning_cash": round(begin_cash, 2), "total_inflows": round(inflow, 2),
            "total_outflows": total_outflows, "pre_revolver_ending_cash": pre_revolver_ending,
            "revolver_draw": draw, "revolver_repay": repay,
            "ending_cash": ending_cash, "ending_revolver_balance": ending_revolver,
        })

        begin_cash = ending_cash
        begin_revolver = ending_revolver

    return (
        pd.DataFrame(activity_rows, columns=ACTIVITY_COLUMNS),
        pd.DataFrame(interest_rows, columns=INTEREST_COLUMNS),
        pd.DataFrame(cash_rows, columns=CASH_POSITION_COLUMNS),
    )


def _empty() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    return (
        pd.DataFrame(columns=ACTIVITY_COLUMNS),
        pd.DataFrame(columns=INTEREST_COLUMNS),
        pd.DataFrame(columns=CASH_POSITION_COLUMNS),
    )


def write_to_sqlite(df: pd.DataFrame, table_name: str) -> int:
    """Replace the table contents with df. Returns the number of rows written."""
    with get_connection() as conn:
        df.to_sql(table_name, conn, if_exists="replace", index=False)
    return len(df)


def _load_weekly_inputs(
    horizon_weeks: int = FORECAST_HORIZON_WEEKS,
) -> tuple[dict[int, float], dict[int, float]]:
    """Read the upstream weekly streams from SQLite into inflow / base-outflow dicts."""
    conn = get_connection()
    try:
        receipts = _week_sums(conn, "combined_receipts_by_week", "receipts")
        disb = _week_sums(conn, "combined_disbursements_by_week", "disbursements")
        payroll = _week_sums(conn, "payroll_by_week", "total_payroll")
        principal = _week_sums(conn, "debt_principal_by_week", "principal")
        interest = _week_sums(conn, "debt_interest_by_week", "interest")
    finally:
        conn.close()

    inflows = {wk: receipts.get(wk, 0.0) for wk in range(1, horizon_weeks + 1)}
    base_outflows = {
        wk: disb.get(wk, 0.0) + payroll.get(wk, 0.0) + principal.get(wk, 0.0) + interest.get(wk, 0.0)
        for wk in range(1, horizon_weeks + 1)
    }
    return inflows, base_outflows


def run(as_of: Optional[dt.date] = None) -> None:
    """Entrypoint: read config + upstream streams, run the plug, write 3 tables."""
    logging.basicConfig(level=LOG_LEVEL, format=LOG_FORMAT)
    if as_of is None:
        as_of = dt.date.today()

    config = load_revolver_config()
    if config is None:
        logger.warning(
            "Revolver config %s not found; writing empty %s / %s / %s (no revolver this run).",
            INPUT_FILE, ACTIVITY_TABLE, INTEREST_TABLE, CASH_POSITION_TABLE,
        )
        activity, interest, cash = _empty()
    else:
        inflows, base_outflows = _load_weekly_inputs()
        activity, interest, cash = compute_revolver(config, inflows, base_outflows, as_of)

    write_to_sqlite(activity, ACTIVITY_TABLE)
    write_to_sqlite(interest, INTEREST_TABLE)
    n = write_to_sqlite(cash, CASH_POSITION_TABLE)

    if not cash.empty:
        logger.info(
            "Revolver: %d weeks; draws=%.2f, repays=%.2f, interest=%.2f; "
            "wk13 ending_cash=%.2f, ending_revolver=%.2f; %d capacity breach(es)",
            n, round(float(activity["revolver_draw"].sum()), 2),
            round(float(activity["revolver_repay"].sum()), 2),
            round(float(activity["revolver_interest_accrued"].sum()), 2),
            float(cash.iloc[-1]["ending_cash"]), float(cash.iloc[-1]["ending_revolver_balance"]),
            int(activity["capacity_breached"].sum()),
        )


if __name__ == "__main__":
    run()
