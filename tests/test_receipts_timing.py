"""Tests for src/calc/receipts_timing.py."""
import datetime as dt

import pandas as pd
import pytest

from src.calc.receipts_timing import (
    METHOD_MASTER_FALLBACK,
    stamp_expected_collection_dates,
)


AS_OF = dt.date(2026, 5, 29)


def _dso_df():
    return pd.DataFrame({
        "customerNumber": ["CUST-A", "CUST-B", "CUST-C"],
        "dso_days":       [40, 45, 1032],
        "dso_method":     ["ratio", "ratio", "ratio"],
    })


def _ar_row(customer, posting_date, amount=1000.0, due_days=30):
    """One-row AR DataFrame matching ar_open_with_due_dates shape."""
    return pd.DataFrame({
        "customerNumber": [customer],
        "postingDate":    [posting_date],
        "documentType":   ["Invoice"],
        "amount":         [amount],
        "dueDays":        [due_days],
        "dueDate":        [None],
    })


def test_future_invoice_is_not_overdue():
    """postingDate + dso_days > as_of -> natural future timing, no clamp."""
    ar = _ar_row("CUST-A", dt.date(2026, 5, 15))

    out = stamp_expected_collection_dates(ar, _dso_df(), AS_OF)

    assert out.iloc[0]["expected_collection_date"] == dt.date(2026, 6, 24)
    assert out.iloc[0]["was_overdue"] == False
    assert out.iloc[0]["days_overdue"] == 0
    assert out.iloc[0]["timing_method"] == "ratio"


def test_overdue_invoice_clamps_to_as_of():
    """postingDate + dso_days < as_of -> clamp to as_of and tag overdue."""
    ar = _ar_row("CUST-A", dt.date(2026, 4, 1))

    out = stamp_expected_collection_dates(ar, _dso_df(), AS_OF)

    assert out.iloc[0]["expected_collection_date"] == AS_OF
    assert out.iloc[0]["was_overdue"] == True
    assert out.iloc[0]["days_overdue"] == 18


def test_on_boundary_invoice_is_not_overdue():
    """postingDate + dso_days exactly == as_of -> not overdue."""
    ar = _ar_row("CUST-A", dt.date(2026, 4, 19))

    out = stamp_expected_collection_dates(ar, _dso_df(), AS_OF)

    assert out.iloc[0]["expected_collection_date"] == AS_OF
    assert out.iloc[0]["was_overdue"] == False
    assert out.iloc[0]["days_overdue"] == 0


def test_severely_aged_customer_dates_far_into_future():
    """DSO=1032 means raw collection lands ~2.8 years out; not overdue.

    The 13-week bucketing layer will naturally exclude these rows, which is
    the right behavior at this stage (AI Tier 3 probability scoring is where
    aged-customer haircuts live, not here).
    """
    ar = _ar_row("CUST-C", dt.date(2025, 11, 1), amount=43790.0)

    out = stamp_expected_collection_dates(ar, _dso_df(), AS_OF)

    expected = dt.date(2025, 11, 1) + dt.timedelta(days=1032)
    assert out.iloc[0]["expected_collection_date"] == expected
    assert out.iloc[0]["was_overdue"] == False


def test_customer_missing_from_dso_falls_back_to_due_days():
    """Rare case: customer in AR but not in DSO table -> use row's dueDays."""
    ar = _ar_row("CUST-Z", dt.date(2026, 5, 1), due_days=30)

    out = stamp_expected_collection_dates(ar, _dso_df(), AS_OF)

    assert out.iloc[0]["expected_collection_date"] == dt.date(2026, 5, 31)
    assert out.iloc[0]["was_overdue"] == False
    assert out.iloc[0]["timing_method"] == METHOD_MASTER_FALLBACK
    assert out.iloc[0]["dso_days_effective"] == 30


def test_multiple_rows_per_customer_stamped_independently():
    """Each invoice gets its own timing based on its own postingDate."""
    ar = pd.DataFrame({
        "customerNumber": ["CUST-A", "CUST-A", "CUST-A"],
        "postingDate": [
            dt.date(2026, 4, 1),
            dt.date(2026, 4, 19),
            dt.date(2026, 5, 15),
        ],
        "documentType": ["Invoice", "Invoice", "Invoice"],
        "amount": [1000.0, 2000.0, 3000.0],
        "dueDays": [30, 30, 30],
        "dueDate": [None, None, None],
    })

    out = stamp_expected_collection_dates(ar, _dso_df(), AS_OF)

    assert out.iloc[0]["was_overdue"] == True
    assert out.iloc[1]["was_overdue"] == False
    assert out.iloc[2]["was_overdue"] == False
    assert out.iloc[2]["expected_collection_date"] == dt.date(2026, 6, 24)


def test_terms_fallback_dso_flows_through_as_timing_method():
    """A customer with dso_method='terms_fallback' should carry that tag through."""
    dso = pd.DataFrame({
        "customerNumber": ["CUST-X"],
        "dso_days":       [30],
        "dso_method":     ["terms_fallback"],
    })
    ar = _ar_row("CUST-X", dt.date(2026, 5, 1))

    out = stamp_expected_collection_dates(ar, dso, AS_OF)

    assert out.iloc[0]["timing_method"] == "terms_fallback"
    assert out.iloc[0]["dso_days_effective"] == 30


def test_overdue_dollar_volume_sums_correctly():
    """Diagnostic check: overdue $$$ should equal sum of clamped row amounts."""
    ar = pd.DataFrame({
        "customerNumber": ["CUST-A", "CUST-A", "CUST-A"],
        "postingDate": [
            dt.date(2026, 4, 1),
            dt.date(2026, 1, 1),
            dt.date(2026, 5, 15),
        ],
        "documentType": ["Invoice"] * 3,
        "amount": [1000.0, 5000.0, 3000.0],
        "dueDays": [30, 30, 30],
        "dueDate": [None, None, None],
    })

    out = stamp_expected_collection_dates(ar, _dso_df(), AS_OF)

    overdue_total = out.loc[out["was_overdue"], "amount"].sum()
    future_total = out.loc[~out["was_overdue"], "amount"].sum()
    assert overdue_total == pytest.approx(6000.0)
    assert future_total == pytest.approx(3000.0)