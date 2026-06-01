"""Tests for src/calc/bucketing.py."""
import datetime as dt

import pandas as pd
import pytest

from src.calc.bucketing import (
    SOURCE_AR,
    SOURCE_SO,
    aggregate_disbursements_by_week,
    aggregate_receipts_by_week,
    assign_forecast_week,
    bucket_so_receipts,
    build_combined_receipts,
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


# ---- AP disbursements: same grid, vendorNumber + disbursement_amount ----------
#
# Mirrors the AR cases above. The week-assignment core is shared via the
# date_col parameter (expected_payment_date instead of expected_collection_date);
# aggregate_disbursements_by_week sums the already-positive disbursement_amount.


def _stamped_ap(*rows):
    """Build a stamped AP DataFrame from (vendor, payment_date, amount) tuples.

    disbursement_amount is positive on both AP streams (see payments_timing.py),
    so these fixtures use positive values like the AR receipts fixtures.
    """
    return pd.DataFrame({
        "vendorNumber": [r[0] for r in rows],
        "expected_payment_date": [r[1] for r in rows],
        "disbursement_amount": [r[2] for r in rows],
        "was_overdue": [False] * len(rows),
    })


def test_ap_assign_forecast_week_basic_bucketing():
    """AP rows bucket onto the same Monday-anchored grid via date_col."""
    stamped = _stamped_ap(
        ("V", dt.date(2026, 5, 29), 100.0),  # week 1
        ("V", dt.date(2026, 6, 1),  200.0),  # week 2
    )

    out = assign_forecast_week(stamped, AS_OF_FRIDAY, date_col="expected_payment_date")

    assert list(out["forecast_week"]) == [1, 2]
    assert list(out["week_start_date"]) == [dt.date(2026, 5, 25), dt.date(2026, 6, 1)]


def test_ap_aggregate_basic_disbursements_by_vendor_week():
    """disbursement_amount aggregates per (vendor, week) into a 'disbursements' col."""
    stamped = _stamped_ap(
        ("V", dt.date(2026, 6, 1), 1000.0),
        ("W", dt.date(2026, 6, 1), 2000.0),
    )
    stamped_w = assign_forecast_week(stamped, AS_OF_FRIDAY, date_col="expected_payment_date")

    agg = aggregate_disbursements_by_week(stamped_w, horizon_weeks=13)

    assert len(agg) == 2
    assert "disbursements" in agg.columns
    by_vendor = agg.set_index("vendorNumber")["disbursements"].to_dict()
    assert by_vendor == {"V": 1000.0, "W": 2000.0}


def test_ap_aggregate_sums_multiple_rows_per_vendor_week():
    """Two rows for the same (vendor, week) collapse to one summed row."""
    stamped = _stamped_ap(
        ("V", dt.date(2026, 5, 29), 1000.0),  # week 1
        ("V", dt.date(2026, 5, 31), 500.0),   # week 1
        ("V", dt.date(2026, 6, 1),  2000.0),  # week 2
    )
    stamped_w = assign_forecast_week(stamped, AS_OF_FRIDAY, date_col="expected_payment_date")

    agg = aggregate_disbursements_by_week(stamped_w, horizon_weeks=13)

    v_w1 = agg[(agg["vendorNumber"] == "V") & (agg["forecast_week"] == 1)]
    v_w2 = agg[(agg["vendorNumber"] == "V") & (agg["forecast_week"] == 2)]
    assert len(agg) == 2
    assert v_w1.iloc[0]["disbursements"] == pytest.approx(1500.0)
    assert v_w2.iloc[0]["disbursements"] == pytest.approx(2000.0)


def test_ap_aggregate_excludes_out_of_horizon_rows():
    """AP rows beyond week 13 are dropped from the aggregate, like AR."""
    stamped = _stamped_ap(
        ("V", dt.date(2026, 6, 1),  1000.0),  # week 2, in horizon
        ("V", dt.date(2026, 8, 24), 2000.0),  # week 14, out of horizon
        ("W", dt.date(2028, 9, 13), 9999.0),  # week 121, out of horizon
    )
    stamped_w = assign_forecast_week(stamped, AS_OF_FRIDAY, date_col="expected_payment_date")

    agg = aggregate_disbursements_by_week(stamped_w, horizon_weeks=13)

    assert len(agg) == 1
    assert agg.iloc[0]["vendorNumber"] == "V"
    assert agg.iloc[0]["forecast_week"] == 2


def test_ap_aggregate_with_empty_input_returns_empty():
    """Defensive: an empty stamped AP DataFrame produces an empty aggregate."""
    stamped = pd.DataFrame({
        "vendorNumber": pd.Series([], dtype="string"),
        "expected_payment_date": pd.Series([], dtype="datetime64[ns]"),
        "disbursement_amount": pd.Series([], dtype="float64"),
        "was_overdue": pd.Series([], dtype="bool"),
    })
    stamped_w = assign_forecast_week(stamped, AS_OF_FRIDAY, date_col="expected_payment_date")

    agg = aggregate_disbursements_by_week(stamped_w, horizon_weeks=13)

    assert len(agg) == 0

# ---- open-SO receipts bucketing + combined view (Task 4) ----------------------


def _so_stamped(*rows):
    """Build a stamped SO frame from (customer, collection_date, outstandingAmount)."""
    return pd.DataFrame({
        "customerNumber": [r[0] for r in rows],
        "expected_collection_date": [r[1] for r in rows],
        "outstandingAmount": [r[2] for r in rows],
        "source_stream": ["open_so"] * len(rows),
    })


def test_so_receipts_bucket_into_correct_forecast_week():
    """SO lines bucket by expected_collection_date, summing outstandingAmount."""
    so = _so_stamped(
        ("CUST-A", dt.date(2026, 5, 29), 1000.0),  # week 1
        ("CUST-A", dt.date(2026, 6, 1),  2000.0),  # week 2
        ("CUST-B", dt.date(2026, 6, 1),  500.0),   # week 2
    )

    agg = bucket_so_receipts(so, AS_OF_FRIDAY, horizon_weeks=13)

    # value column is named "receipts" to match the AR side
    assert "receipts" in agg.columns
    a_w1 = agg[(agg["customerNumber"] == "CUST-A") & (agg["forecast_week"] == 1)]
    a_w2 = agg[(agg["customerNumber"] == "CUST-A") & (agg["forecast_week"] == 2)]
    assert a_w1.iloc[0]["receipts"] == pytest.approx(1000.0)
    assert a_w2.iloc[0]["receipts"] == pytest.approx(2000.0)


def _ar_by_week(*rows):
    return pd.DataFrame({
        "customerNumber": [r[0] for r in rows],
        "forecast_week": [r[1] for r in rows],
        "week_start_date": [r[2] for r in rows],
        "receipts": [r[3] for r in rows],
    })


def test_combined_view_row_count_is_ar_plus_so():
    ar = _ar_by_week(("CUST-A", 1, dt.date(2026, 5, 25), 100.0),
                     ("CUST-B", 2, dt.date(2026, 6, 1), 200.0))
    so = _ar_by_week(("CUST-A", 3, dt.date(2026, 6, 8), 300.0))

    combined = build_combined_receipts(ar, so)

    assert len(combined) == len(ar) + len(so)  # 3


def test_combined_view_preserves_source_discriminator():
    ar = _ar_by_week(("CUST-A", 1, dt.date(2026, 5, 25), 100.0))
    so = _ar_by_week(("CUST-A", 1, dt.date(2026, 5, 25), 300.0))

    combined = build_combined_receipts(ar, so)

    assert set(combined["source"]) == {SOURCE_AR, SOURCE_SO}


def test_customer_in_both_streams_has_two_rows_not_summed():
    """Same customer+week in both streams stays as two rows (one per source)."""
    ar = _ar_by_week(("CUST-A", 1, dt.date(2026, 5, 25), 100.0))
    so = _ar_by_week(("CUST-A", 1, dt.date(2026, 5, 25), 300.0))

    combined = build_combined_receipts(ar, so)

    cust_a = combined[combined["customerNumber"] == "CUST-A"]
    assert len(cust_a) == 2
    assert sorted(cust_a["receipts"].tolist()) == [100.0, 300.0]  # not a single 400.0


def test_combined_view_empty_so_is_just_ar():
    ar = _ar_by_week(("CUST-A", 1, dt.date(2026, 5, 25), 100.0))
    empty_so = pd.DataFrame(columns=["customerNumber", "forecast_week", "week_start_date", "receipts"])

    combined = build_combined_receipts(ar, empty_so)

    assert len(combined) == 1
    assert combined.iloc[0]["source"] == SOURCE_AR


# ---- open-PO payments bucketing + combined disbursements (Phase 5) ------------


def _po_stamped(*rows):
    """Build a stamped PO frame from (vendor, source_stream, payment_date, amount)."""
    return pd.DataFrame({
        "vendorNumber": [r[0] for r in rows],
        "source_stream": [r[1] for r in rows],
        "expected_payment_date": [r[2] for r in rows],
        "amount": [r[3] for r in rows],
    })


def test_po_payments_bucket_into_correct_week_and_keep_source_stream():
    from src.calc.bucketing import bucket_po_payments
    po = _po_stamped(
        ("V1", "po_rbni",        dt.date(2026, 5, 29), 1000.0),  # week 1
        ("V1", "po_outstanding", dt.date(2026, 6, 1),  2000.0),  # week 2
        ("V1", "po_rbni",        dt.date(2026, 6, 1),  500.0),   # week 2
    )
    agg = bucket_po_payments(po, AS_OF_FRIDAY, horizon_weeks=13)

    assert "disbursements" in agg.columns
    # week 2 keeps po_rbni and po_outstanding as SEPARATE rows
    wk2 = agg[agg["forecast_week"] == 2].set_index("source_stream")["disbursements"].to_dict()
    assert wk2["po_outstanding"] == pytest.approx(2000.0)
    assert wk2["po_rbni"] == pytest.approx(500.0)


def _ap_by_week(*rows):
    return pd.DataFrame({
        "vendorNumber": [r[0] for r in rows],
        "forecast_week": [r[1] for r in rows],
        "week_start_date": [r[2] for r in rows],
        "disbursements": [r[3] for r in rows],
    })


def _po_by_week(*rows):
    return pd.DataFrame({
        "vendorNumber": [r[0] for r in rows],
        "source_stream": [r[1] for r in rows],
        "forecast_week": [r[2] for r in rows],
        "week_start_date": [r[3] for r in rows],
        "disbursements": [r[4] for r in rows],
    })


def test_combined_disbursements_is_true_union_no_mutation():
    from src.calc.bucketing import build_combined_disbursements, SOURCE_AP
    ap = _ap_by_week(("V1", 1, dt.date(2026, 5, 25), 100.0),
                     ("V2", 2, dt.date(2026, 6, 1), 200.0))
    po = _po_by_week(("V1", "po_rbni", 1, dt.date(2026, 5, 25), 300.0))
    ap_before = ap.copy()

    combined = build_combined_disbursements(ap, po)

    assert len(combined) == len(ap) + len(po)  # 3
    assert set(combined["source"]) == {SOURCE_AP, "po_rbni"}
    # AP frame not mutated (no 'source' column leaked back in).
    assert "source" not in ap.columns
    pd.testing.assert_frame_equal(ap, ap_before)


def test_combined_disbursements_graceful_when_po_empty():
    """PO missing -> combined equals AP rows (tagged open_ap), no double-count."""
    from src.calc.bucketing import build_combined_disbursements, SOURCE_AP
    ap = _ap_by_week(("V1", 1, dt.date(2026, 5, 25), 100.0))
    empty_po = pd.DataFrame(columns=["vendorNumber", "source_stream", "forecast_week", "week_start_date", "disbursements"])

    combined = build_combined_disbursements(ap, empty_po)

    assert len(combined) == 1
    assert combined.iloc[0]["source"] == SOURCE_AP
    assert combined.iloc[0]["disbursements"] == pytest.approx(100.0)
