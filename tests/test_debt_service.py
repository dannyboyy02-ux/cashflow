"""Tests for src/calc/debt_service.py."""
import datetime as dt
import json
import logging

import holidays
import pandas as pd
import pytest

from src.calc import debt_service
from src.calc.debt_service import (
    INTEREST_TABLE,
    PRINCIPAL_TABLE,
    SOURCE_INTEREST,
    SOURCE_PRINCIPAL,
    build_debt_by_week,
    run,
    same_day_next_month,
    shift_to_business_day,
)


# Friday as-of -> week 1 Monday = 2026-05-25; but the seed loans pay on the 1st.
# Use a Monday as-of so week 1 Monday = 2026-06-01 (matches the live sanity run).
AS_OF = dt.date(2026, 6, 1)


def _loan(name="L1", balance=1_200_000, rate=0.12, monthly=100_000, next_pay="2026-06-01"):
    return {
        "name": name,
        "balance_outstanding": balance,
        "annual_interest_rate": rate,
        "monthly_principal_payment": monthly,
        "next_payment_date": next_pay,
    }


def _schedule(*loans):
    return {"loans": list(loans)}


def _principal_week(df, week):
    return float(df[df["forecast_week"] == week]["principal"].sum())


def _interest_week(df, week):
    return float(df[df["forecast_week"] == week]["interest"].sum())


# ---- pure date helpers ------------------------------------------------------


def test_shift_weekend_saturday_to_monday():
    us = holidays.UnitedStates(years=[2026])
    # 2026-08-01 is a Saturday -> 2026-08-03 Monday.
    assert shift_to_business_day(dt.date(2026, 8, 1), us) == dt.date(2026, 8, 3)


def test_shift_holiday_new_years_to_monday():
    us = holidays.UnitedStates(years=[2027])
    # 2027-01-01 is Friday AND New Year's Day -> next business day Mon 1/4/27.
    assert shift_to_business_day(dt.date(2027, 1, 1), us) == dt.date(2027, 1, 4)


def test_same_day_next_month_rolls_year():
    assert same_day_next_month(dt.date(2026, 6, 1)) == dt.date(2026, 7, 1)
    assert same_day_next_month(dt.date(2026, 12, 1)) == dt.date(2027, 1, 1)


# ---- steady weekday payment -------------------------------------------------


def test_steady_weekday_payment_principal_and_interest():
    # 2026-06-01 is a Monday (weekday). interest = 1.2M * 0.12/12 = 12,000.
    principal, interest = build_debt_by_week(_schedule(_loan()), AS_OF)

    assert _principal_week(principal, 1) == pytest.approx(100_000)
    assert _interest_week(interest, 1) == pytest.approx(12_000.0)
    assert principal[principal["forecast_week"] == 1].iloc[0]["source_stream"] == SOURCE_PRINCIPAL
    assert interest[interest["forecast_week"] == 1].iloc[0]["source_stream"] == SOURCE_INTEREST


# ---- weekend shift moves the payment a week later ---------------------------


def test_weekend_shift_lands_in_week_10_not_9():
    # Loan first pays 2026-08-01 (Saturday). Unshifted -> wk 9; shifted 8/3 -> wk 10.
    loan = _loan(name="WKND", balance=500_000, monthly=100_000, next_pay="2026-08-01")
    principal, _ = build_debt_by_week(_schedule(loan), AS_OF)

    weeks = set(principal["forecast_week"])
    assert 10 in weeks
    assert 9 not in weeks


# ---- holiday shift ----------------------------------------------------------


def test_holiday_shift_payment_date_is_jan_4():
    # as_of late 2026 so 1/1/27 is in-horizon; New Year's Day (Fri) -> Mon 1/4/27.
    as_of = dt.date(2026, 12, 7)  # Monday -> week 1 Monday = 2026-12-07
    loan = _loan(name="NYD", balance=500_000, monthly=100_000, next_pay="2027-01-01")
    principal, _ = build_debt_by_week(_schedule(loan), as_of)

    # 1/1/27 (Fri+holiday) -> 1/4/27 (Mon). week 1 Monday 12/7; 1/4 is 28 days -> wk 5.
    row = principal.iloc[0]
    assert row["week_start_date"] == dt.date(2027, 1, 4)
    assert row["forecast_week"] == 5


# ---- interest declines as principal pays down -------------------------------


def test_interest_decreases_month_over_month():
    # One loan paying in wks 1, 5, 10; interest on a shrinking balance declines.
    principal, interest = build_debt_by_week(_schedule(_loan(balance=1_200_000)), AS_OF)

    by_week = interest.sort_values("forecast_week")["interest"].tolist()
    assert by_week == sorted(by_week, reverse=True)   # strictly non-increasing
    assert by_week[0] > by_week[-1]


# ---- loan payoff: final partial payment, loop exits -------------------------


def test_loan_payoff_final_payment_is_partial():
    # 250k balance, 100k/mo -> 100k, 100k, 50k (partial), then balance 0 -> stop.
    loan = _loan(name="PAYOFF", balance=250_000, monthly=100_000, next_pay="2026-06-01")
    principal, _ = build_debt_by_week(_schedule(loan), AS_OF)

    paid = principal.sort_values("forecast_week")["principal"].tolist()
    assert paid == pytest.approx([100_000, 100_000, 50_000])
    assert sum(paid) == pytest.approx(250_000)  # never overpays the balance


# ---- multiple loans aggregate within a week ---------------------------------


def test_multiple_loans_aggregate_in_same_week():
    principal, _ = build_debt_by_week(
        _schedule(
            _loan(name="A", monthly=100_000),
            _loan(name="B", monthly=40_000),
        ),
        AS_OF,
    )
    # Loan-name granularity: two rows in wk 1, summing to 140,000.
    wk1 = principal[principal["forecast_week"] == 1]
    assert len(wk1) == 2
    assert _principal_week(principal, 1) == pytest.approx(140_000)


# ---- run() / file handling --------------------------------------------------


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    import src.db as db
    monkeypatch.setattr(db, "SQLITE_PATH", tmp_path / "test.db")
    return tmp_path / "test.db"


def test_run_writes_both_streams(tmp_db, tmp_path, monkeypatch):
    input_file = tmp_path / "debt_schedule.json"
    input_file.write_text(json.dumps(_schedule(_loan())))
    monkeypatch.setattr(debt_service, "INPUT_FILE", input_file)

    run(as_of=AS_OF)

    from src.db import get_connection
    conn = get_connection()
    assert conn.execute(f"SELECT COUNT(*) FROM {PRINCIPAL_TABLE}").fetchone()[0] > 0
    assert conn.execute(f"SELECT COUNT(*) FROM {INTEREST_TABLE}").fetchone()[0] > 0
    conn.close()


def test_run_missing_file_warns_writes_empty_no_exception(tmp_db, tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(debt_service, "INPUT_FILE", tmp_path / "missing.json")

    with caplog.at_level(logging.WARNING):
        run(as_of=AS_OF)  # must not raise

    assert any("not found" in r.message for r in caplog.records)
    from src.db import get_connection
    conn = get_connection()
    assert conn.execute(f"SELECT COUNT(*) FROM {PRINCIPAL_TABLE}").fetchone()[0] == 0
    assert conn.execute(f"SELECT COUNT(*) FROM {INTEREST_TABLE}").fetchone()[0] == 0
    conn.close()
