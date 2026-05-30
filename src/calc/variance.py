"""Compute week-level day-over-day variance from the forecast snapshots.

Compares the most recent snapshot ("today") against the most recent snapshot
that precedes it ("prior"), aggregating across customers/vendors to a week-level
view (the workbook variance sheet is week-level, not entity-level).

  ar_receipts_snapshots       \\
                               >--  forecast_variance  (today vs prior, per week)
  ap_disbursements_snapshots  /

Net convention: net = AR receipts - AP disbursements. So a RISE in AP
disbursements (more cash out) lowers net_today and produces a NEGATIVE
net_delta -- correctly flagging that more committed outflow is bad for cash.

Edge cases:
  - No prior snapshot (first run ever, or only one snapshot_date present):
    the output table is empty (the workbook then shows a "no comparison"
    placeholder). Not an error.
  - Prior snapshot more than 7 calendar days old: log a warning but proceed;
    the comparison is still meaningful, just stale.

Design note: rows are keyed on forecast_week (the stable relative-horizon index)
and merged across snapshots on that key; week_start_date is carried from the
current snapshot (falling back to prior). Within any single snapshot
forecast_week and week_start_date are 1:1, so this matches the spec's
"(forecast_week, week_start_date) level" while staying robust if the as-of date
advanced across a Monday boundary between snapshots.

forecast_variance is a current-state table (today vs prior), rewritten each run.
"""
from __future__ import annotations

import datetime as dt
import logging
import sqlite3
from typing import Optional

import pandas as pd

from src.config import LOG_LEVEL, LOG_FORMAT
from src.db import get_connection
from src.calc.snapshot import AR_SNAPSHOT_TABLE, AP_SNAPSHOT_TABLE

logger = logging.getLogger(__name__)

OUTPUT_TABLE = "forecast_variance"

STALE_PRIOR_DAYS = 7

VARIANCE_COLUMNS = [
    "snapshot_date", "prior_snapshot_date", "forecast_week", "week_start_date",
    "ar_receipts_today", "ar_receipts_prior", "ar_receipts_delta",
    "ap_disbursements_today", "ap_disbursements_prior", "ap_disbursements_delta",
    "net_today", "net_prior", "net_delta",
]


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    cur = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    )
    return cur.fetchone() is not None


def load_table(name: str) -> pd.DataFrame:
    """Read a table, or return an empty frame if it doesn't exist yet."""
    with get_connection() as conn:
        if not _table_exists(conn, name):
            return pd.DataFrame()
        return pd.read_sql(f"SELECT * FROM {name}", conn)


def _empty_variance() -> pd.DataFrame:
    return pd.DataFrame(columns=VARIANCE_COLUMNS)


def _week_sums(snap: pd.DataFrame, snapshot_date: str, value_col: str, out_col: str) -> pd.DataFrame:
    """Aggregate one snapshot date's entity rows to (forecast_week, week_start_date)."""
    d = snap[snap["snapshot_date"] == snapshot_date]
    if d.empty:
        return pd.DataFrame(columns=["forecast_week", "week_start_date", out_col])
    return (
        d.groupby(["forecast_week", "week_start_date"], as_index=False)[value_col]
        .sum()
        .rename(columns={value_col: out_col})
    )


def compute_variance(
    ar_snap: pd.DataFrame,
    ap_snap: pd.DataFrame,
    current_snapshot_date: Optional[str] = None,
) -> pd.DataFrame:
    """Return the week-level variance of the current snapshot vs the prior one.

    current_snapshot_date defaults to the latest snapshot_date present. Returns
    an empty (correctly-columned) frame when there is no prior snapshot.
    """
    dates: set[str] = set()
    for snap in (ar_snap, ap_snap):
        if not snap.empty and "snapshot_date" in snap.columns:
            dates.update(snap["snapshot_date"].unique())
    if not dates:
        return _empty_variance()

    current = current_snapshot_date or max(dates)
    priors = sorted(d for d in dates if d < current)
    if not priors:
        return _empty_variance()
    prior = priors[-1]  # most recent prior, regardless of gap size

    try:
        gap = (dt.date.fromisoformat(current) - dt.date.fromisoformat(prior)).days
        if gap > STALE_PRIOR_DAYS:
            logger.warning(
                "Prior snapshot %s is %d days before current %s (> %d); "
                "variance is stale but still computed.",
                prior, gap, current, STALE_PRIOR_DAYS,
            )
    except (TypeError, ValueError):
        pass  # non-ISO date strings: skip the staleness check, still compute

    ar_t = _week_sums(ar_snap, current, "receipts", "ar_receipts_today")
    ar_p = _week_sums(ar_snap, prior, "receipts", "ar_receipts_prior")
    ap_t = _week_sums(ap_snap, current, "disbursements", "ap_disbursements_today")
    ap_p = _week_sums(ap_snap, prior, "disbursements", "ap_disbursements_prior")

    ar_t_map = dict(zip(ar_t["forecast_week"], ar_t["ar_receipts_today"]))
    ar_p_map = dict(zip(ar_p["forecast_week"], ar_p["ar_receipts_prior"]))
    ap_t_map = dict(zip(ap_t["forecast_week"], ap_t["ap_disbursements_today"]))
    ap_p_map = dict(zip(ap_p["forecast_week"], ap_p["ap_disbursements_prior"]))

    # week_start_date keyed by forecast_week; prefer the current snapshot's value.
    ws_map: dict = {}
    for frame in (ar_t, ap_t, ar_p, ap_p):
        for fw, wsd in zip(frame["forecast_week"], frame["week_start_date"]):
            ws_map.setdefault(fw, wsd)

    all_weeks = sorted(
        set(ar_t_map) | set(ar_p_map) | set(ap_t_map) | set(ap_p_map)
    )

    rows = []
    for fw in all_weeks:
        art = float(ar_t_map.get(fw, 0.0))
        arp = float(ar_p_map.get(fw, 0.0))
        apt = float(ap_t_map.get(fw, 0.0))
        app = float(ap_p_map.get(fw, 0.0))
        net_t = art - apt
        net_p = arp - app
        rows.append({
            "snapshot_date": current,
            "prior_snapshot_date": prior,
            "forecast_week": int(fw),
            "week_start_date": ws_map.get(fw),
            "ar_receipts_today": art,
            "ar_receipts_prior": arp,
            "ar_receipts_delta": art - arp,
            "ap_disbursements_today": apt,
            "ap_disbursements_prior": app,
            "ap_disbursements_delta": apt - app,
            "net_today": net_t,
            "net_prior": net_p,
            "net_delta": net_t - net_p,
        })
    return pd.DataFrame(rows, columns=VARIANCE_COLUMNS)


def write_to_sqlite(df: pd.DataFrame, table_name: str = OUTPUT_TABLE) -> int:
    """Replace the variance table (current-state, not history). Returns row count."""
    with get_connection() as conn:
        df.to_sql(table_name, conn, if_exists="replace", index=False)
    return len(df)


def run(current_snapshot_date: Optional[str] = None) -> None:
    """Entrypoint: load snapshots, compute variance, write the result table."""
    logging.basicConfig(level=LOG_LEVEL, format=LOG_FORMAT)
    ar_snap = load_table(AR_SNAPSHOT_TABLE)
    ap_snap = load_table(AP_SNAPSHOT_TABLE)

    var = compute_variance(ar_snap, ap_snap, current_snapshot_date)
    n = write_to_sqlite(var)

    if var.empty:
        logger.info(
            "No prior snapshot available; wrote empty %s (variance needs >= 2 "
            "snapshot dates).", OUTPUT_TABLE,
        )
    else:
        logger.info(
            "Computed variance for snapshot_date=%s vs prior=%s across %d weeks; "
            "wrote %d rows to %s",
            var["snapshot_date"].iloc[0], var["prior_snapshot_date"].iloc[0],
            n, n, OUTPUT_TABLE,
        )


if __name__ == "__main__":
    run()
