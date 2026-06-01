"""Project a weekly payroll disbursement stream onto the 13-week grid.

Payroll is a separate treasury disbursement category, NOT part of A/P. It lives
in UKG with no API access, so it is a manually-maintained assumption read from a
gitignored JSON input (it holds real Latitude figures; the repo is public):

    inputs/payroll_input.json
    {
      "default_weekly_gross": <steady weekly gross wages>,
      "employer_burden_pct":  <ER taxes + 401k match etc., as a fraction>,
      "overrides": {"YYYY-MM-DD": <gross for that Monday-anchored week>, ...}
    }

For each of the 13 forecast weeks (Monday-anchored, reusing the bucketing grid
anchor monday_of_week -- the grid is NOT re-derived here):

    gross         = overrides[monday] if present else default_weekly_gross
    total_payroll = round(gross * (1 + employer_burden_pct), 2)

Output table payroll_by_week (forecast_week, week_start_date, gross_wages,
employer_burden_pct, total_payroll, source_stream="payroll"). gross_wages and
employer_burden_pct are retained for audit; total_payroll is the cash figure.

This is a single extract+calc module (the input is one small JSON, so the
house extract/calc split would be overkill). It deliberately does NOT touch the
Excel writer, snapshot, or combined_disbursements_by_week -- payroll is its own
forecast line, and rendering/variance integration is a later phase. Graceful: a
missing input file logs a WARNING and writes an empty payroll_by_week so the
rest of the pipeline still runs.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path
from typing import Optional

import pandas as pd

from src.config import LOG_LEVEL, LOG_FORMAT, INPUTS_DIR, FORECAST_HORIZON_WEEKS
from src.db import get_connection
from src.calc.bucketing import monday_of_week

logger = logging.getLogger(__name__)

INPUT_FILE = INPUTS_DIR / "payroll_input.json"
OUTPUT_TABLE = "payroll_by_week"
SOURCE_STREAM = "payroll"

OUTPUT_COLUMNS = [
    "forecast_week", "week_start_date", "gross_wages",
    "employer_burden_pct", "total_payroll", "source_stream",
]


def load_payroll_input(path: Optional[Path] = None) -> Optional[dict]:
    """Read the payroll input JSON, or return None if the file is absent."""
    path = Path(path) if path is not None else INPUT_FILE
    if not path.exists():
        return None
    with open(path) as f:
        return json.load(f)


def build_payroll_by_week(
    payroll_cfg: dict,
    as_of: dt.date,
    horizon_weeks: int = FORECAST_HORIZON_WEEKS,
) -> pd.DataFrame:
    """Build the 13-week payroll stream from the input config.

    Iterates the Monday-anchored grid (week 1 = week containing as_of). An
    override keyed to a week's Monday (YYYY-MM-DD) replaces the default gross for
    that week; overrides not matching any in-window Monday are simply never
    looked up, so out-of-window overrides are ignored.
    """
    default_gross = float(payroll_cfg.get("default_weekly_gross", 0.0))
    burden = float(payroll_cfg.get("employer_burden_pct", 0.0))
    overrides = payroll_cfg.get("overrides") or {}

    week_1_monday = monday_of_week(as_of)
    rows = []
    for i in range(horizon_weeks):
        monday = week_1_monday + dt.timedelta(days=7 * i)
        gross = float(overrides.get(monday.isoformat(), default_gross))
        rows.append({
            "forecast_week": i + 1,
            "week_start_date": monday,
            "gross_wages": gross,
            "employer_burden_pct": burden,
            "total_payroll": round(gross * (1 + burden), 2),
            "source_stream": SOURCE_STREAM,
        })
    return pd.DataFrame(rows, columns=OUTPUT_COLUMNS)


def _empty_payroll() -> pd.DataFrame:
    return pd.DataFrame(columns=OUTPUT_COLUMNS)


def write_to_sqlite(df: pd.DataFrame, table_name: str = OUTPUT_TABLE) -> int:
    """Replace the table contents with df. Returns the number of rows written."""
    with get_connection() as conn:
        df.to_sql(table_name, conn, if_exists="replace", index=False)
    return len(df)


def run(as_of: Optional[dt.date] = None) -> None:
    """Entrypoint: read payroll input, build the weekly stream, write the table."""
    logging.basicConfig(level=LOG_LEVEL, format=LOG_FORMAT)
    if as_of is None:
        as_of = dt.date.today()

    cfg = load_payroll_input()
    if cfg is None:
        logger.warning(
            "Payroll input %s not found; writing empty %s (no payroll line this run).",
            INPUT_FILE, OUTPUT_TABLE,
        )
        write_to_sqlite(_empty_payroll())
        return

    df = build_payroll_by_week(cfg, as_of)
    n = write_to_sqlite(df)
    logger.info(
        "Payroll: %d weeks, default_weekly_gross=%.2f, burden=%.4f, %d override(s); "
        "total_payroll sum=%.2f -> %s",
        n, float(cfg.get("default_weekly_gross", 0.0)),
        float(cfg.get("employer_burden_pct", 0.0)),
        len(cfg.get("overrides") or {}),
        round(float(df["total_payroll"].sum()), 2), OUTPUT_TABLE,
    )


if __name__ == "__main__":
    run()
