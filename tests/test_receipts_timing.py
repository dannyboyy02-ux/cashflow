"""Tests for src/calc/receipts_timing.py."""
import datetime as dt

import pandas as pd
import pytest

from src.calc.receipts_timing import (
    METHOD_MASTER_FALLBACK,
    RECEIPT_DOC_TYPES,
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
    # CUST-A DSO=40, posting 5/15 -> expected 6/24
    ar = _ar_row("CUST-A", dt.date(2026, 5, 15))

    out = stamp_expected_collection_dates(ar, _dso_df(), AS_OF)

    assert out.iloc[0]["expected_collection_date"] == dt.date(2026, 6, 24)
    assert out.iloc[0]["was_overdue"] == False
    assert out.iloc[0]["days_overdue"] == 0
    assert out.iloc[0]["timing_method"] == "ratio"


def test_overdue_invoice_clamps_to_as_of():
    """postingDate + dso_days < as_of -> clamp to as_of and tag overdue."""
    # CUST-A DSO=40, posting 1/1 -> would be 2/10; clamps to 5/29
    ar = _ar_row("CUST-A", dt.date(2026, 1, 1))

    out = stamp_expected_collection_dates(ar, _dso_df(), AS_OF)

    assert out.iloc[0]["expected_collection_date"] == AS_OF
    assert out.iloc[0]["was_overdue"] == True
    # 5/29 - 2/10 = 108 days
    assert out.iloc[0]["days_overdue"] == 108


def test_on_boundary_invoice_is_not_overdue():
    """postingDate + dso_days == as_of -> not overdue, no clamp."""
    # CUST-A DSO=40, posting 4/19 -> expected 5/29 == AS_OF
    ar = _ar_row("CUST-A", dt.date(2026, 4, 19))

    out = stamp_expected_collection_dates(ar, _dso_df(), AS_OF)

    assert out.iloc[0]["expected_collection_date"] == AS_OF
    assert out.iloc[0]["was_overdue"] == False
    assert out.iloc[0]["days_overdue"] == 0


def test_severely_aged_customer_dates_far_into_future():
    """The 1032-day DSO outlier stamps a 2029 expected_collection_date.

    The bucketing layer is responsible for treating out-of-horizon entries.
    receipts_timing.py just stamps the math honestly.
    """
    ar = _ar_row("CUST-C", dt.date(2026, 5, 15))

    out = stamp_expected_collection_dates(ar, _dso_df(), AS_OF)

    # 2026-05-15 + 1032 days = 2029-03-12
    assert out.iloc[0]["expected_collection_date"] == dt.date(2029, 3, 12)
    assert out.iloc[0]["was_overdue"] == False


def test_customer_missing_from_dso_falls_back_to_due_days():
    """Customer in AR but missing from DSO table -> use the row's dueDays."""
    ar = _ar_row("MYSTERY", dt.date(2026, 5, 15), due_days=15)

    out = stamp_expected_collection_dates(ar, _dso_df(), AS_OF)

    # 2026-05-15 + 15 = 2026-05-30
    assert out.iloc[0]["expected_collection_date"] == dt.date(2026, 5, 30)
    assert out.iloc[0]["timing_method"] == METHOD_MASTER_FALLBACK
    assert out.iloc[0]["dso_days_effective"] == 15


def test_multiple_rows_per_customer_stamped_independently():
    ar = pd.DataFrame({
        "customerNumber": ["CUST-A", "CUST-A", "CUST-B"],
        "postingDate":    [dt.date(2026, 5, 15), dt.date(2026, 1, 1), dt.date(2026, 5, 1)],
        "documentType":   ["Invoice", "Invoice", "Invoice"],
        "amount":         [1000.0, 2000.0, 3000.0],
        "dueDays":        [30, 30, 30],
        "dueDate":        [None, None, None],
    })

    out = stamp_expected_collection_dates(ar, _dso_df(), AS_OF)

    # First A row: 5/15 + 40 = 6/24, not overdue
    assert out.iloc[0]["expected_collection_date"] == dt.date(2026, 6, 24)
    assert out.iloc[0]["was_overdue"] == False
    # Second A row: overdue, clamps
    assert out.iloc[1]["expected_collection_date"] == AS_OF
    assert out.iloc[1]["was_overdue"] == True
    # B row: 5/1 + 45 = 6/15, not overdue
    assert out.iloc[2]["expected_collection_date"] == dt.date(2026, 6, 15)
    assert out.iloc[2]["was_overdue"] == False


def test_terms_fallback_dso_flows_through_as_timing_method():
    """If the DSO row carries terms_fallback method, that flows through."""
    dso_df = pd.DataFrame({
        "customerNumber": ["CUST-X"],
        "dso_days":       [30],
        "dso_method":     ["terms_fallback"],
    })
    ar = _ar_row("CUST-X", dt.date(2026, 5, 15))

    out = stamp_expected_collection_dates(ar, dso_df, AS_OF)

    assert out.iloc[0]["timing_method"] == "terms_fallback"


def test_overdue_dollar_volume_sums_correctly():
    """Quick check that overdue/future splits aggregate cleanly."""
    ar = pd.DataFrame({
        "customerNumber": ["CUST-A", "CUST-A", "CUST-A"],
        "postingDate":    [dt.date(2026, 5, 15), dt.date(2026, 1, 1), dt.date(2025, 12, 1)],
        "documentType":   ["Invoice", "Invoice", "Invoice"],
        "amount":         [3000.0, 2000.0, 4000.0],
        "dueDays":        [30, 30, 30],
        "dueDate":        [None, None, None],
    })

    out = stamp_expected_collection_dates(ar, _dso_df(), AS_OF)

    overdue_total = out.loc[out["was_overdue"], "amount"].sum()
    future_total = out.loc[~out["was_overdue"], "amount"].sum()
    assert overdue_total == pytest.approx(6000.0)
    assert future_total == pytest.approx(3000.0)


# ---- documentType filter: non-Invoice rows excluded from receipts forecast ----


def _multi_doctype_ar():
    """An open AR table containing all four document types for the same customer.

    Mirrors the real shape of bc_customer_ledger_entries: open invoices coexist
    with the occasional open Payment (unapplied), open Credit Memo (unapplied),
    and (rare) open Refund. Only Invoice should land in the receipts forecast.
    """
    return pd.DataFrame({
        "customerNumber": ["CUST-A"] * 4,
        "postingDate":    [dt.date(2026, 5, 1)] * 4,
        "documentType":   ["Invoice", "Payment", "Credit Memo", "Refund"],
        "amount":         [10_000.0, -3_000.0, -500.0, 200.0],
        "dueDays":        [30] * 4,
        "dueDate":        [None] * 4,
    })


def test_receipt_doc_types_constant_is_invoice_only():
    """Sanity check that the documentType filter is exactly Invoice.

    Payments, credit memos, and refunds are economic events that don't
    represent future cash inflows -- see module docstring.
    """
    assert set(RECEIPT_DOC_TYPES) == {"Invoice"}


def test_stamp_excludes_open_payments_from_receipts():
    """An unapplied open Payment is cash already received; not a future receipt."""
    out = stamp_expected_collection_dates(_multi_doctype_ar(), _dso_df(), AS_OF)

    # Only the Invoice row survives
    assert len(out) == 1
    assert out.iloc[0]["documentType"] == "Invoice"
    assert out.iloc[0]["amount"] == pytest.approx(10_000.0)


def test_stamp_excludes_open_credit_memos_from_receipts():
    """An unapplied credit memo reduces AR but isn't a future receipt."""
    ar = pd.DataFrame({
        "customerNumber": ["CUST-A"],
        "postingDate":    [dt.date(2026, 5, 1)],
        "documentType":   ["Credit Memo"],
        "amount":         [-500.0],
        "dueDays":        [30],
        "dueDate":        [None],
    })

    out = stamp_expected_collection_dates(ar, _dso_df(), AS_OF)

    assert len(out) == 0


def test_stamp_excludes_blank_doctype_rows():
    """Rare manual journal entries with blank documentType are not receipts."""
    ar = pd.DataFrame({
        "customerNumber": ["CUST-A"],
        "postingDate":    [dt.date(2026, 5, 1)],
        "documentType":   [None],
        "amount":         [1_000.0],
        "dueDays":        [30],
        "dueDate":        [None],
    })

    out = stamp_expected_collection_dates(ar, _dso_df(), AS_OF)

    assert len(out) == 0


def test_stamp_preserves_invoice_rows_when_mixed_with_non_receipts():
    """Multiple invoices mixed with non-receipt rows -> all invoices flow through."""
    ar = pd.DataFrame({
        "customerNumber": ["CUST-A", "CUST-B", "CUST-A", "CUST-A"],
        "postingDate":    [dt.date(2026, 5, 1)] * 4,
        "documentType":   ["Invoice", "Invoice", "Payment", "Credit Memo"],
        "amount":         [10_000.0, 5_000.0, -3_000.0, -200.0],
        "dueDays":        [30] * 4,
        "dueDate":        [None] * 4,
    })

    out = stamp_expected_collection_dates(ar, _dso_df(), AS_OF)

    # Both invoices preserved, payment + credit memo dropped
    assert len(out) == 2
    assert set(out["documentType"].unique()) == {"Invoice"}
    assert out["amount"].sum() == pytest.approx(15_000.0)