"""Tests for src/calc/bucketing.py."""
import datetime as dt

import pandas as pd
import pytest

from src.calc.bucketing import (
    aggregate_receipts_by_week,
    assign_forecast_week,
    monday_of_week,
)


# ---- monday_of_week: the calendar-week anchor ---------------------------------

@pytest.mark.parametrize("date_in,expected_monday", [
    (dt.date(2026, 5, 25), dt.date(2026, 5, 25)),  # Monday -> itself
    (dt.date(2026, 5, 26), dt.date(2026, 5, 25)),  # Tuesday
    (dt.date(2026, 5, 27), dt.date(2026, 5, 25)),  # Wednesday
    (dt.date(2026, 5, 28), dt.date(2026, 5, 25)),  # Thursday
    (dt.date(2026, 5, 29), dt.date(2026, 5, 25)),  # Friday
    (dt.date(2026, 5, 30), dt.date(2026, 5, 25)),  # Saturday
    (dt.date(2026, 5, 31), dt.date(2026, 5, 25)),  # Sunday
    (dt.date(2026, 6, 1),  dt.date(2026, 6, 1)),   # next Monday
])
def test_monday_of_week_returns_calendar_week_anchor(date_in, expected_monday):
    assert monday_of_week(date_in) == expected_monday


# ---- assign_forecast_week: per-row week assignment ----------------------------

AS_OF_FRIDAY = dt.date(2026, 5, 29)   # week 1 Monday = 2026-05-25
AS_OF_MONDAY = dt.date(2026, 6, 1)    # week 1 Monday = 2026-06-01


def _stamped(*rows):
    """Build a stamped DataFrame from (customer, collection_date, amount) tuples."""
    return pd.DataFrame({
        "customerNumber": [r[0] for r in rows],
        "expected_collection_date": [r[1] for r in rows],
        "amount": [r[2] for r in rows],
        "was_overdue": [False] * len(rows),
    })


def test_assign_forecast_week_handles_week_1_through_week_2_boundary():
    """5/29-5/31 -> week 1; 6/1 -> week 2."""
    stamped = _stamped(
        ("A", dt.date(2026, 5, 29), 100.0),
        ("A", dt.date(2026, 5, 31), 200.0),
        ("A", dt.date(2026, 6, 1),  300.0),
    )

    out = assign_forecast_week(stamped, AS_OF_FRIDAY)

    assert list(out["forecast_week"]) == [1, 1, 2]
    assert list(out["week_start_date"]) == [
        dt.date(2026, 5, 25),
        dt.date(2026, 5, 25),
        dt.date(2026, 6, 1),
    ]


def test_assign_forecast_week_handles_week_13_boundary():
    """Week 13 spans 8/17-8/23; 8/24 is week 14 (out of horizon)."""
    stamped = _stamped(
        ("A", dt.date(2026, 8, 17), 100.0),  # Monday of week 13
        ("A", dt.date(2026, 8, 23), 200.0),  # Sunday of week 13
        ("A", dt.date(2026, 8, 24), 300.0),  # Monday of week 14
    )

    out = assign_forecast_week(stamped, AS_OF_FRIDAY)

    assert list(out["forecast_week"]) == [13, 13, 14]


def test_assign_forecast_week_with_monday_as_of():
    """When as_of_date is itself a Monday, that day is the start of week 1."""
    stamped = _stamped(
        ("A", dt.date(2026, 6, 1), 100.0),  # as_of = week 1 Mon
        ("A", dt.date(2026, 6, 7), 200.0),  # Sun of week 1
        ("A", dt.date(2026, 6, 8), 300.0),  # Mon of week 2
    )

    out = assign_forecast_week(stamped, AS_OF_MONDAY)

    assert list(out["forecast_week"]) == [1, 1, 2]
    assert out.iloc[0]["week_start_date"] == AS_OF_MONDAY


def test_assign_forecast_week_overdue_clamped_lands_in_week_1():
    """Overdue rows have expected_collection_date == as_of -> always week 1."""
    stamped = _stamped(
        ("A", AS_OF_FRIDAY, 1000.0),
    )

    out = assign_forecast_week(stamped, AS_OF_FRIDAY)

    assert out.iloc[0]["forecast_week"] == 1


def test_assign_forecast_week_far_future_returns_large_week_number():
    """Far-future dates (severely-aged customer) yield forecast_week >> 13."""
    stamped = _stamped(
        ("A", dt.date(2028, 9, 13), 43790.0),  # 842 days out
    )

    out = assign_forecast_week(stamped, AS_OF_FRIDAY)

    # 842 days / 7 = 120, +1 = 121
    assert out.iloc[0]["forecast_week"] == 121


# ---- aggregate_receipts_by_week: groupby + horizon filter ---------------------

def test_aggregate_sums_multiple_rows_per_customer_week():
    """Two rows for the same (customer, week) collapse to one with summed amount."""
    stamped = _stamped(
        ("A", dt.date(2026, 5, 29), 1000.0),
        ("A", dt.date(2026, 5, 31), 500.0),
        ("A", dt.date(2026, 6, 1),  2000.0),
    )
    stamped_w = assign_forecast_week(stamped, AS_OF_FRIDAY)

    agg = aggregate_receipts_by_week(stamped_w, horizon_weeks=13)

    a_w1 = agg[(agg["customerNumber"] == "A") & (agg["forecast_week"] == 1)]
    a_w2 = agg[(agg["customerNumber"] == "A") & (agg["forecast_week"] == 2)]
    assert len(agg) == 2
    assert a_w1.iloc[0]["receipts"] == pytest.approx(1500.0)
    assert a_w2.iloc[0]["receipts"] == pytest.approx(2000.0)


def test_aggregate_excludes_out_of_horizon_rows():
    """Rows with forecast_week > horizon_weeks are dropped from the aggregate."""
    stamped = _stamped(
        ("A", dt.date(2026, 6, 1),  1000.0),
        ("A", dt.date(2026, 8, 24), 2000.0),  # week 14
        ("B", dt.date(2028, 9, 13), 43790.0), # week 121
    )
    stamped_w = assign_forecast_week(stamped, AS_OF_FRIDAY)

    agg = aggregate_receipts_by_week(stamped_w, horizon_weeks=13)

    assert len(agg) == 1
    assert agg.iloc[0]["customerNumber"] == "A"
    assert agg.iloc[0]["forecast_week"] == 2


def test_aggregate_keeps_customers_separate():
    """Two customers in the same week get separate output rows."""
    stamped = _stamped(
        ("A", dt.date(2026, 6, 1), 1000.0),
        ("B", dt.date(2026, 6, 1), 2000.0),
    )
    stamped_w = assign_forecast_week(stamped, AS_OF_FRIDAY)

    agg = aggregate_receipts_by_week(stamped_w, horizon_weeks=13)

    assert len(agg) == 2
    by_cust = agg.set_index("customerNumber")["receipts"].to_dict()
    assert by_cust == {"A": 1000.0, "B": 2000.0}


def test_aggregate_respects_custom_horizon():
    """Horizon parameter changes which rows are in/out."""
    stamped = _stamped(
        ("A", dt.date(2026, 6, 1),  1000.0),  # week 2
        ("A", dt.date(2026, 6, 22), 2000.0),  # week 5
    )
    stamped_w = assign_forecast_week(stamped, AS_OF_FRIDAY)

    agg_h13 = aggregate_receipts_by_week(stamped_w, horizon_weeks=13)
    agg_h3 = aggregate_receipts_by_week(stamped_w, horizon_weeks=3)

    assert len(agg_h13) == 2
    assert len(agg_h3) == 1
    assert agg_h3.iloc[0]["forecast_week"] == 2


def test_aggregate_with_empty_input_returns_empty():
    """Defensive: an empty stamped DataFrame produces an empty aggregate."""
    stamped = pd.DataFrame({
        "customerNumber": pd.Series([], dtype="string"),
        "expected_collection_date": pd.Series([], dtype="datetime64[ns]"),
        "amount": pd.Series([], dtype="float64"),
        "was_overdue": pd.Series([], dtype="bool"),
    })
    stamped_w = assign_forecast_week(stamped, AS_OF_FRIDAY)

    agg = aggregate_receipts_by_week(stamped_w, horizon_weeks=13)

    assert len(agg) == 0