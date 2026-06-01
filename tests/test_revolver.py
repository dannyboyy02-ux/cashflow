"""Tests for src/calc/revolver.py -- sequential revolver plug + cash position."""
import datetime as dt
import json
import logging

import pandas as pd
import pytest

from src.calc import revolver
from src.calc.revolver import (
    ACTIVITY_TABLE,
    CASH_POSITION_TABLE,
    INTEREST_TABLE,
    SOURCE_INTEREST,
    compute_revolver,
    run,
)


AS_OF = dt.date(2026, 6, 1)  # Monday -> week 1 Monday = 2026-06-01


def _config(beginning_cash=0, facility_total=60_000_000, lc_carve_out=2_545_000,
            current_drawn_balance=0, minimum_cash_target=1_500_000,
            sofr_rate=0.0535, spread=0.0135):
    return {
        "beginning_cash": beginning_cash,
        "facility_total": facility_total,
        "lc_carve_out": lc_carve_out,
        "current_drawn_balance": current_drawn_balance,
        "minimum_cash_target": minimum_cash_target,
        "sofr_rate": sofr_rate,
        "spread": spread,
    }


def _flat(value, weeks=13):
    return {wk: float(value) for wk in range(1, weeks + 1)}


# ---- beginning anchor -------------------------------------------------------


def test_week1_beginning_anchors_to_config():
    cfg = _config(beginning_cash=750_000, current_drawn_balance=4_000_000)
    _, _, cash = compute_revolver(cfg, _flat(0), _flat(0), AS_OF)
    activity_rows = compute_revolver(cfg, _flat(0), _flat(0), AS_OF)[0]

    assert cash.iloc[0]["beginning_cash"] == pytest.approx(750_000)
    assert activity_rows.iloc[0]["begin_revolver_balance"] == pytest.approx(4_000_000)


# ---- sequential state carry -------------------------------------------------


def test_week2_begin_equals_week1_ending():
    # No draw/repay churn: start above target, no balance -> cash just accumulates.
    cfg = _config(beginning_cash=2_000_000, current_drawn_balance=0)
    inflows = {1: 500_000, 2: 0.0}
    outflows = {1: 0.0, 2: 0.0}
    activity, _, cash = compute_revolver(cfg, inflows, outflows, AS_OF, horizon_weeks=2)

    assert cash.iloc[1]["beginning_cash"] == pytest.approx(cash.iloc[0]["ending_cash"])
    assert activity.iloc[1]["begin_revolver_balance"] == pytest.approx(activity.iloc[0]["ending_revolver_balance"])


def test_13_week_chain_consistency():
    cfg = _config(beginning_cash=1_500_000, current_drawn_balance=1_000_000)
    # Alternating cash-rich / cash-poor weeks to exercise draws and repays.
    inflows = {wk: (5_000_000 if wk % 2 else 100_000) for wk in range(1, 14)}
    outflows = {wk: (200_000 if wk % 2 else 3_000_000) for wk in range(1, 14)}
    activity, _, cash = compute_revolver(cfg, inflows, outflows, AS_OF)

    assert len(cash) == 13
    for wk in range(2, 14):
        prev = cash.iloc[wk - 2]
        cur = cash.iloc[wk - 1]
        assert cur["beginning_cash"] == pytest.approx(prev["ending_cash"])
        assert activity.iloc[wk - 1]["begin_revolver_balance"] == pytest.approx(
            activity.iloc[wk - 2]["ending_revolver_balance"])


# ---- draw scenario ----------------------------------------------------------


def test_draw_to_target_when_below():
    cfg = _config(beginning_cash=0, current_drawn_balance=0, minimum_cash_target=1_500_000)
    # wk1: inflow 1M, outflow 5M -> pre = -4M < target -> draw to 1.5M.
    activity, _, cash = compute_revolver(cfg, {1: 1_000_000}, {1: 5_000_000}, AS_OF, horizon_weeks=1)
    row = cash.iloc[0]

    assert row["pre_revolver_ending_cash"] == pytest.approx(-4_000_000)
    assert row["revolver_draw"] == pytest.approx(5_500_000)   # -4M -> 1.5M
    assert row["revolver_repay"] == pytest.approx(0.0)
    assert row["ending_cash"] == pytest.approx(1_500_000)
    assert activity.iloc[0]["ending_revolver_balance"] == pytest.approx(5_500_000)
    assert bool(activity.iloc[0]["capacity_breached"]) is False


# ---- repay scenario ---------------------------------------------------------


def test_repay_from_excess_when_above_target_with_balance():
    cfg = _config(beginning_cash=1_500_000, current_drawn_balance=3_000_000)
    # wk1: inflow 5M, outflow 1M -> pre = 5.5M; excess over 1.5M = 4M; repay min(4M, 3M) = 3M.
    activity, _, cash = compute_revolver(cfg, {1: 5_000_000}, {1: 1_000_000}, AS_OF, horizon_weeks=1)
    row = cash.iloc[0]

    # interest accrues on opening 3M balance, so pre is slightly under 5.5M.
    assert row["revolver_repay"] == pytest.approx(3_000_000)   # bounded by balance
    assert row["revolver_draw"] == pytest.approx(0.0)
    assert activity.iloc[0]["ending_revolver_balance"] == pytest.approx(0.0)
    # ending sits above target (excess exceeded the balance we could repay).
    assert row["ending_cash"] > 1_500_000


def test_repay_bounded_lands_at_target_when_balance_small():
    cfg = _config(beginning_cash=1_500_000, current_drawn_balance=10_000_000)
    # excess (4M) < balance (10M) -> repay exactly the excess, ending == target.
    _, _, cash = compute_revolver(cfg, {1: 5_000_000}, {1: 1_000_000}, AS_OF, horizon_weeks=1)
    row = cash.iloc[0]
    # interest on 10M reduces pre below 5.5M, so repay = pre - target and ending == target.
    assert row["ending_cash"] == pytest.approx(1_500_000)
    assert row["revolver_repay"] == pytest.approx(row["pre_revolver_ending_cash"] - 1_500_000)


# ---- no-action scenario -----------------------------------------------------


def test_no_action_above_target_with_zero_balance():
    cfg = _config(beginning_cash=2_000_000, current_drawn_balance=0)
    activity, _, cash = compute_revolver(cfg, {1: 1_000_000}, {1: 0.0}, AS_OF, horizon_weeks=1)
    row = cash.iloc[0]

    assert row["revolver_draw"] == pytest.approx(0.0)
    assert row["revolver_repay"] == pytest.approx(0.0)
    assert row["ending_cash"] == pytest.approx(3_000_000)   # sits above target
    assert activity.iloc[0]["ending_revolver_balance"] == pytest.approx(0.0)


# ---- capacity breach --------------------------------------------------------


def test_capacity_breach_clamps_draw():
    # max_capacity = 60M - 2.545M = 57.455M. Outflow far exceeds it.
    cfg = _config(beginning_cash=0, current_drawn_balance=0)
    activity, _, cash = compute_revolver(cfg, {1: 0.0}, {1: 100_000_000}, AS_OF, horizon_weeks=1)
    row = cash.iloc[0]

    assert bool(activity.iloc[0]["capacity_breached"]) is True
    assert activity.iloc[0]["ending_revolver_balance"] == pytest.approx(57_455_000)  # maxed out
    assert row["revolver_draw"] == pytest.approx(57_455_000)
    assert row["ending_cash"] < 1_500_000   # could not reach target


# ---- interest accrual -------------------------------------------------------


def test_revolver_interest_accrues_on_opening_balance():
    cfg = _config(beginning_cash=1_500_000, current_drawn_balance=5_200_000)
    activity, interest, cash = compute_revolver(cfg, {1: 0.0}, {1: 0.0}, AS_OF, horizon_weeks=1)

    weekly_rate = (0.0535 + 0.0135) / 52
    expected_int = round(5_200_000 * weekly_rate, 2)
    assert activity.iloc[0]["revolver_interest_accrued"] == pytest.approx(expected_int)
    assert interest.iloc[0]["interest"] == pytest.approx(expected_int)
    assert interest.iloc[0]["source_stream"] == SOURCE_INTEREST
    # interest is folded into total_outflows (base 0 + interest).
    assert cash.iloc[0]["total_outflows"] == pytest.approx(expected_int)


def test_zero_balance_zero_interest():
    cfg = _config(current_drawn_balance=0)
    activity, _, _ = compute_revolver(cfg, {1: 0.0}, {1: 0.0}, AS_OF, horizon_weeks=1)
    assert activity.iloc[0]["revolver_interest_accrued"] == pytest.approx(0.0)


# ---- run() / file handling --------------------------------------------------


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    import src.db as db
    monkeypatch.setattr(db, "SQLITE_PATH", tmp_path / "test.db")
    return tmp_path / "test.db"


def test_run_reads_db_and_writes_three_tables(tmp_db, tmp_path, monkeypatch):
    # Seed minimal upstream tables.
    from src.db import get_connection
    conn = get_connection()
    pd.DataFrame({"customerNumber": ["C"], "forecast_week": [1], "week_start_date": ["2026-06-01"],
                  "receipts": [1_000_000.0], "source": ["open_ar"]}).to_sql(
        "combined_receipts_by_week", conn, if_exists="replace", index=False)
    pd.DataFrame({"vendorNumber": ["V"], "forecast_week": [1], "week_start_date": ["2026-06-01"],
                  "disbursements": [5_000_000.0], "source": ["open_ap"]}).to_sql(
        "combined_disbursements_by_week", conn, if_exists="replace", index=False)
    conn.commit()
    conn.close()

    cfg_file = tmp_path / "revolver_config.json"
    cfg_file.write_text(json.dumps(_config()))
    monkeypatch.setattr(revolver, "INPUT_FILE", cfg_file)

    run(as_of=AS_OF)

    conn = get_connection()
    for t in (ACTIVITY_TABLE, INTEREST_TABLE, CASH_POSITION_TABLE):
        assert conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] == 13
    # wk1: inflow 1M, outflow 5M, begin 0 -> draws to target 1.5M.
    wk1 = conn.execute(f"SELECT ending_cash, revolver_draw FROM {CASH_POSITION_TABLE} WHERE forecast_week=1").fetchone()
    conn.close()
    assert wk1[0] == pytest.approx(1_500_000)
    assert wk1[1] > 0


def test_run_missing_file_warns_writes_empty_no_exception(tmp_db, tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(revolver, "INPUT_FILE", tmp_path / "missing.json")

    with caplog.at_level(logging.WARNING):
        run(as_of=AS_OF)  # must not raise

    assert any("not found" in r.message for r in caplog.records)
    from src.db import get_connection
    conn = get_connection()
    for t in (ACTIVITY_TABLE, INTEREST_TABLE, CASH_POSITION_TABLE):
        assert conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] == 0
    conn.close()
