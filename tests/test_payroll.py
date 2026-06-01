"""Tests for src/calc/payroll.py."""
import datetime as dt
import json
import logging

import pandas as pd
import pytest

from src.calc import payroll
from src.calc.payroll import (
    OUTPUT_TABLE,
    SOURCE_STREAM,
    build_payroll_by_week,
    run,
)


# Friday as-of -> week 1 Monday = 2026-05-25; week N Monday = 2026-05-25 + 7*(N-1).
AS_OF = dt.date(2026, 5, 29)
WEEK1_MONDAY = dt.date(2026, 5, 25)


def _monday(week: int) -> dt.date:
    return WEEK1_MONDAY + dt.timedelta(days=7 * (week - 1))


def _cfg(default_gross=2_088_910, burden=0.12, overrides=None):
    return {
        "default_weekly_gross": default_gross,
        "employer_burden_pct": burden,
        "overrides": overrides or {},
    }


# ---------------------------------------------------------------------------


def test_steady_week_total_is_gross_times_burden():
    df = build_payroll_by_week(_cfg(), AS_OF)

    row = df.iloc[0]
    assert row["gross_wages"] == pytest.approx(2_088_910)
    assert row["total_payroll"] == pytest.approx(2_088_910 * 1.12)  # 2,339,579.20
    assert row["total_payroll"] == pytest.approx(2_339_579.20)
    assert row["source_stream"] == SOURCE_STREAM


def test_all_13_weeks_present():
    df = build_payroll_by_week(_cfg(), AS_OF)

    assert len(df) == 13
    assert list(df["forecast_week"]) == list(range(1, 14))
    assert list(df.columns) == [
        "forecast_week", "week_start_date", "gross_wages",
        "employer_burden_pct", "total_payroll", "source_stream",
    ]
    # week_start_date is the Monday-anchored grid.
    assert df.iloc[0]["week_start_date"] == WEEK1_MONDAY
    assert df.iloc[12]["week_start_date"] == _monday(13)


def test_override_week_uses_override_gross():
    over_monday = _monday(3)  # 2026-06-08
    df = build_payroll_by_week(_cfg(overrides={over_monday.isoformat(): 4_500_000}), AS_OF)

    wk3 = df[df["forecast_week"] == 3].iloc[0]
    assert wk3["gross_wages"] == pytest.approx(4_500_000)
    assert wk3["total_payroll"] == pytest.approx(4_500_000 * 1.12)
    # Other weeks stay on the default.
    wk2 = df[df["forecast_week"] == 2].iloc[0]
    assert wk2["gross_wages"] == pytest.approx(2_088_910)


def test_override_outside_window_is_ignored():
    df = build_payroll_by_week(_cfg(overrides={"2030-01-07": 9_999_999}), AS_OF)

    # No in-window week takes the out-of-range override; all weeks default.
    assert (df["gross_wages"] == 2_088_910).all()


def test_zero_burden_total_equals_gross():
    df = build_payroll_by_week(_cfg(burden=0.0), AS_OF)

    row = df.iloc[0]
    assert row["total_payroll"] == pytest.approx(row["gross_wages"])
    assert row["total_payroll"] == pytest.approx(2_088_910)


def test_override_on_week_1_monday():
    """Sanity: an override pinned to week-1 Monday lands on week 1."""
    df = build_payroll_by_week(_cfg(overrides={WEEK1_MONDAY.isoformat(): 3_000_000}), AS_OF)
    assert df.iloc[0]["gross_wages"] == pytest.approx(3_000_000)
    assert df.iloc[1]["gross_wages"] == pytest.approx(2_088_910)


# ---- run() / file handling --------------------------------------------------


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    import src.db as db
    monkeypatch.setattr(db, "SQLITE_PATH", tmp_path / "test.db")
    return tmp_path / "test.db"


def test_run_writes_all_13_weeks_to_table(tmp_db, tmp_path, monkeypatch):
    input_file = tmp_path / "payroll_input.json"
    input_file.write_text(json.dumps(_cfg()))
    monkeypatch.setattr(payroll, "INPUT_FILE", input_file)

    run(as_of=AS_OF)

    from src.db import get_connection
    conn = get_connection()
    n = conn.execute(f"SELECT COUNT(*) FROM {OUTPUT_TABLE}").fetchone()[0]
    total = conn.execute(f"SELECT ROUND(SUM(total_payroll),2) FROM {OUTPUT_TABLE}").fetchone()[0]
    conn.close()
    assert n == 13
    assert total == pytest.approx(13 * 2_339_579.20)


def test_run_missing_file_warns_writes_empty_no_exception(tmp_db, tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(payroll, "INPUT_FILE", tmp_path / "does_not_exist.json")

    with caplog.at_level(logging.WARNING):
        run(as_of=AS_OF)  # must not raise

    assert any("not found" in r.message for r in caplog.records)
    from src.db import get_connection
    conn = get_connection()
    assert conn.execute(f"SELECT COUNT(*) FROM {OUTPUT_TABLE}").fetchone()[0] == 0
    conn.close()
