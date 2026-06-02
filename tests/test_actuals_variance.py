"""Tests for src/calc/actuals_variance.py."""
import datetime as dt
import json

import pandas as pd
import pytest

from src.calc import actuals_variance as av
from src.calc.actuals_variance import (
    OUTPUT_TABLE,
    compute_forecast_vs_actual,
    load_actuals,
    run,
)


WK1 = "2026-06-01"   # Monday
WK2 = "2026-06-08"


def _ar_snap(*rows):
    """Rows: (snapshot_date, forecast_week, week_start_date, receipts)."""
    return pd.DataFrame({
        "snapshot_date": [r[0] for r in rows],
        "as_of_date": [r[0] for r in rows],
        "customerNumber": ["CUST-A"] * len(rows),
        "forecast_week": [r[1] for r in rows],
        "week_start_date": [r[2] for r in rows],
        "receipts": [r[3] for r in rows],
    })


def _ap_snap(*rows):
    return pd.DataFrame({
        "snapshot_date": [r[0] for r in rows],
        "as_of_date": [r[0] for r in rows],
        "vendorNumber": ["VEND-A"] * len(rows),
        "forecast_week": [r[1] for r in rows],
        "week_start_date": [r[2] for r in rows],
        "disbursements": [r[3] for r in rows],
    })


# ---------------------------------------------------------------------------


def test_basic_grade_one_closed_week():
    """Forecast standing entering WK1 vs actuals recorded for WK1."""
    # WK1's week-1 forecast was taken in the snapshot dated WK1.
    ar = _ar_snap((WK1, 1, WK1, 1_000_000.0), (WK1, 2, WK2, 500_000.0))
    ap = _ap_snap((WK1, 1, WK1, 600_000.0))
    actuals = {WK1: {"receipts": 1_100_000.0, "disbursements": 550_000.0}}

    out = compute_forecast_vs_actual(ar, ap, actuals)

    assert len(out) == 1
    row = out.iloc[0]
    assert row["forecast_receipts"] == pytest.approx(1_000_000.0)
    assert row["actual_receipts"] == pytest.approx(1_100_000.0)
    assert row["receipts_variance"] == pytest.approx(100_000.0)        # collected more
    assert row["forecast_disbursements"] == pytest.approx(600_000.0)
    assert row["disbursements_variance"] == pytest.approx(-50_000.0)   # paid less
    # net forecast 400k, actual 550k -> +150k
    assert row["forecast_net"] == pytest.approx(400_000.0)
    assert row["actual_net"] == pytest.approx(550_000.0)
    assert row["net_variance"] == pytest.approx(150_000.0)


def test_uses_week1_not_later_horizon_weeks():
    """Forecast for a week is the week-1 view, not that week as a far-out week."""
    # A snapshot from an earlier week shows WK2 as its forecast_week 2 (a distant
    # estimate). The snapshot taken AT WK2 shows WK2 as week 1 (the real basis).
    ar = _ar_snap(
        ("2026-05-25", 2, WK2, 999_999.0),   # WK2 seen as week 2 from prior week (ignore)
        (WK2, 1, WK2, 700_000.0),            # WK2 seen as week 1 entering WK2 (use this)
    )
    ap = _ap_snap((WK2, 1, WK2, 0.0))
    out = compute_forecast_vs_actual(ar, ap, {WK2: {"receipts": 720_000.0, "disbursements": 0.0}})
    assert out.iloc[0]["forecast_receipts"] == pytest.approx(700_000.0)


def test_earliest_snapshot_used_when_multiple_in_week():
    """If the pipeline ran twice in a week, the forecast 'entering' it is the earliest."""
    ar = _ar_snap(
        ("2026-06-01", 1, WK1, 1_000_000.0),   # entering the week
        ("2026-06-03", 1, WK1, 1_050_000.0),   # mid-week refresh (ignore for grading)
    )
    ap = _ap_snap(("2026-06-01", 1, WK1, 0.0))
    out = compute_forecast_vs_actual(ar, ap, {WK1: {"receipts": 1_010_000.0, "disbursements": 0.0}})
    assert out.iloc[0]["forecast_snapshot_date"] == "2026-06-01"
    assert out.iloc[0]["forecast_receipts"] == pytest.approx(1_000_000.0)


def test_week_with_no_forecast_snapshot_is_skipped():
    ar = _ar_snap((WK1, 1, WK1, 1_000_000.0))
    ap = _ap_snap((WK1, 1, WK1, 0.0))
    # Actual recorded for a week that has no standing forecast snapshot.
    out = compute_forecast_vs_actual(ar, ap, {"2026-07-13": {"receipts": 5.0, "disbursements": 0.0}})
    assert out.empty


def test_no_actuals_yields_empty():
    ar = _ar_snap((WK1, 1, WK1, 1_000_000.0))
    ap = _ap_snap((WK1, 1, WK1, 0.0))
    out = compute_forecast_vs_actual(ar, ap, {})
    assert out.empty
    assert list(out.columns) == av.OUTPUT_COLUMNS


def test_multiple_weeks_graded_and_sorted():
    ar = _ar_snap((WK1, 1, WK1, 100.0), (WK2, 1, WK2, 200.0))
    ap = _ap_snap((WK1, 1, WK1, 10.0), (WK2, 1, WK2, 20.0))
    actuals = {WK2: {"receipts": 210.0, "disbursements": 25.0},
               WK1: {"receipts": 90.0, "disbursements": 10.0}}
    out = compute_forecast_vs_actual(ar, ap, actuals)
    assert list(out["week_start_date"]) == [WK1, WK2]   # sorted
    assert out.iloc[0]["receipts_variance"] == pytest.approx(-10.0)
    assert out.iloc[1]["receipts_variance"] == pytest.approx(10.0)


def test_load_actuals_missing_file_is_empty(tmp_path):
    assert load_actuals(tmp_path / "nope.json") == {}


def test_load_actuals_reads_json(tmp_path):
    p = tmp_path / "actuals.json"
    p.write_text(json.dumps({WK1: {"receipts": 1.0, "disbursements": 2.0}}))
    assert load_actuals(p) == {WK1: {"receipts": 1.0, "disbursements": 2.0}}


def test_run_writes_table(tmp_path, monkeypatch):
    import src.db as db
    monkeypatch.setattr(db, "SQLITE_PATH", tmp_path / "test.db")
    from src.db import get_connection
    conn = get_connection()
    _ar_snap((WK1, 1, WK1, 1_000_000.0)).to_sql("ar_receipts_snapshots", conn, if_exists="replace", index=False)
    _ap_snap((WK1, 1, WK1, 600_000.0)).to_sql("ap_disbursements_snapshots", conn, if_exists="replace", index=False)
    conn.commit(); conn.close()

    actuals_file = tmp_path / "actuals.json"
    actuals_file.write_text(json.dumps({WK1: {"receipts": 1_100_000.0, "disbursements": 550_000.0}}))
    monkeypatch.setattr(av, "INPUT_FILE", actuals_file)

    run()
    out = av.load_table(OUTPUT_TABLE)
    assert len(out) == 1
    assert out.iloc[0]["net_variance"] == pytest.approx(150_000.0)
