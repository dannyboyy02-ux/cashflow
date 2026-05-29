"""Tests for src/calc/payments_timing.py."""
import datetime as dt

import pandas as pd
import pytest

from src.calc.payments_timing import (
    DISBURSEMENT_DOC_TYPES,
    METHOD_MASTER_FALLBACK,
    METHOD_SCHEDULED,
    SCHEDULED_DOC_TYPE,
    STREAM_ESTIMATED,
    STREAM_SCHEDULED,
    build_disbursements,
    build_scheduled_payments,
    stamp_expected_payment_dates,
)


AS_OF = dt.date(2026, 5, 29)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _dpo_df():
    """Per-vendor DPO lookup matching bc_vendor_dpo shape."""
    return pd.DataFrame({
        "vendorNumber": ["VEND-A", "VEND-B", "VEND-C"],
        "dpo_days":     [14, 30, 1032],
        "dpo_method":   ["ratio", "ratio", "ratio"],
    })


def _ap_row(vendor, posting_date, amount=-1000.0, due_days=30, doc_type="Invoice"):
    """One-row open-AP DataFrame matching ap_open_with_due_dates shape.

    AP Invoice amounts are NEGATIVE (we owe the vendor) -- see sign convention.
    """
    return pd.DataFrame({
        "vendorNumber": [vendor],
        "postingDate":  [posting_date],
        "documentType": [doc_type],
        "amount":       [amount],
        "currencyCode": ["USD"],
        "entryNumber":  [1001],
        "dueDays":      [due_days],
        "dueDate":      [None],
    })


def _history_payment(vendor, posting_date, amount, entry=5001):
    """One pre-scheduled Payment row matching bc_ap_history shape.

    AP Payment amounts are POSITIVE (cash going out reduces what we owe).
    """
    return pd.DataFrame({
        "vendorNumber": [vendor],
        "postingDate":  [posting_date],
        "documentType": ["Payment"],
        "amount":       [amount],
        "currencyCode": ["USD"],
        "entryNumber":  [entry],
    })


# ===========================================================================
# Stream A -- build_scheduled_payments
# ===========================================================================


def test_scheduled_returns_future_dated_payments():
    """Future-dated Payment rows are kept and stamped verbatim."""
    history = _history_payment("VEND-A", dt.date(2026, 6, 1), 9000.0)

    out = build_scheduled_payments(history, as_of=AS_OF)

    assert len(out) == 1
    assert out.iloc[0]["expected_payment_date"] == dt.date(2026, 6, 1)


def test_scheduled_drops_past_dated_payments():
    """A Payment dated on/before as_of is already cash out -- not forecast."""
    history = pd.concat([
        _history_payment("VEND-A", dt.date(2026, 6, 1), 9000.0, entry=1),   # future
        _history_payment("VEND-A", dt.date(2026, 5, 1), 5000.0, entry=2),   # past
        _history_payment("VEND-A", AS_OF, 7000.0, entry=3),                  # == as_of
    ], ignore_index=True)

    out = build_scheduled_payments(history, as_of=AS_OF)

    assert len(out) == 1
    assert out.iloc[0]["entryNumber"] == 1
    assert out.iloc[0]["expected_payment_date"] == dt.date(2026, 6, 1)


def test_scheduled_excludes_non_payment_doctypes():
    """Only Payment rows are scheduled; Invoice/Credit Memo/Refund/blank dropped.

    Covers the 2028/2036 reversing book pairs (future-dated Invoice + Credit
    Memo) -- the documentType filter excludes them with no extra logic.
    """
    history = pd.DataFrame({
        "vendorNumber": ["VEND-A"] * 5,
        "postingDate":  [dt.date(2026, 6, 1)] * 5,
        "documentType": ["Payment", "Invoice", "Credit Memo", "Refund", None],
        "amount":       [9000.0, -2400.0, 2400.0, 100.0, 50.0],
        "currencyCode": ["USD"] * 5,
        "entryNumber":  [1, 2, 3, 4, 5],
    })

    out = build_scheduled_payments(history, as_of=AS_OF)

    assert len(out) == 1
    assert out.iloc[0]["documentType"] == "Payment"


def test_scheduled_returns_empty_cleanly_when_no_future_payments():
    """No future-dated Payments -> zero-row frame, columns still present."""
    history = _history_payment("VEND-A", dt.date(2026, 5, 1), 5000.0)  # past only

    out = build_scheduled_payments(history, as_of=AS_OF)

    assert len(out) == 0
    for col in ("expected_payment_date", "timing_method", "was_overdue",
                "days_overdue", "disbursement_amount", "source_stream"):
        assert col in out.columns


def test_scheduled_stamps_expected_payment_date_equals_posting_date():
    """No transformation -- the scheduled date IS the posting date."""
    history = _history_payment("VEND-A", dt.date(2026, 7, 15), 1234.0)

    out = build_scheduled_payments(history, as_of=AS_OF)

    assert out.iloc[0]["expected_payment_date"] == dt.date(2026, 7, 15)
    assert out.iloc[0]["postingDate"] == dt.date(2026, 7, 15)


def test_scheduled_metadata_columns():
    """timing_method, source_stream, was_overdue, days_overdue on Stream A."""
    history = _history_payment("VEND-A", dt.date(2026, 6, 1), 9000.0)

    out = build_scheduled_payments(history, as_of=AS_OF)
    row = out.iloc[0]

    assert row["timing_method"] == METHOD_SCHEDULED
    assert row["source_stream"] == STREAM_SCHEDULED
    assert row["was_overdue"] == False
    assert row["days_overdue"] == 0
    assert row["dpo_days_effective"] == 0


def test_scheduled_disbursement_amount_positive_equals_amount():
    """AP Payment amounts are already positive; disbursement_amount == amount."""
    history = _history_payment("VEND-A", dt.date(2026, 6, 1), 9000.0)

    out = build_scheduled_payments(history, as_of=AS_OF)

    assert out.iloc[0]["disbursement_amount"] == pytest.approx(9000.0)
    assert out.iloc[0]["amount"] == pytest.approx(9000.0)


def test_scheduled_doc_type_constant():
    assert SCHEDULED_DOC_TYPE == "Payment"


# ===========================================================================
# Stream B -- stamp_expected_payment_dates
# ===========================================================================


def test_disbursement_doc_types_constant_is_invoice_only():
    """The Stream B documentType filter is exactly Invoice."""
    assert set(DISBURSEMENT_DOC_TYPES) == {"Invoice"}


def test_future_invoice_is_not_overdue():
    """postingDate + dpo_days > as_of -> natural future timing, no clamp."""
    # VEND-A DPO=14, posting 5/20 -> expected 6/3
    ap = _ap_row("VEND-A", dt.date(2026, 5, 20))

    out = stamp_expected_payment_dates(ap, _dpo_df(), AS_OF)

    assert out.iloc[0]["expected_payment_date"] == dt.date(2026, 6, 3)
    assert out.iloc[0]["was_overdue"] == False
    assert out.iloc[0]["days_overdue"] == 0
    assert out.iloc[0]["timing_method"] == "ratio"


def test_overdue_invoice_clamps_to_as_of():
    """postingDate + dpo_days < as_of -> clamp to as_of and tag overdue."""
    # VEND-A DPO=14, posting 1/1 -> would be 1/15; clamps to 5/29
    ap = _ap_row("VEND-A", dt.date(2026, 1, 1))

    out = stamp_expected_payment_dates(ap, _dpo_df(), AS_OF)

    assert out.iloc[0]["expected_payment_date"] == AS_OF
    assert out.iloc[0]["was_overdue"] == True
    # 5/29 - 1/15 = 134 days
    assert out.iloc[0]["days_overdue"] == 134


def test_on_boundary_invoice_is_not_overdue():
    """postingDate + dpo_days == as_of -> not overdue, no clamp."""
    # VEND-A DPO=14, posting 5/15 -> expected 5/29 == AS_OF
    ap = _ap_row("VEND-A", dt.date(2026, 5, 15))

    out = stamp_expected_payment_dates(ap, _dpo_df(), AS_OF)

    assert out.iloc[0]["expected_payment_date"] == AS_OF
    assert out.iloc[0]["was_overdue"] == False
    assert out.iloc[0]["days_overdue"] == 0


def test_severely_aged_vendor_dates_far_into_future():
    """A 1032-day DPO outlier stamps a 2029 expected_payment_date, uncapped."""
    ap = _ap_row("VEND-C", dt.date(2026, 5, 15))

    out = stamp_expected_payment_dates(ap, _dpo_df(), AS_OF)

    # 2026-05-15 + 1032 days = 2029-03-12
    assert out.iloc[0]["expected_payment_date"] == dt.date(2029, 3, 12)
    assert out.iloc[0]["was_overdue"] == False


def test_vendor_missing_from_dpo_falls_back_to_due_days():
    """Vendor in AP but missing from DPO table -> use the row's dueDays."""
    ap = _ap_row("MYSTERY", dt.date(2026, 5, 15), due_days=15)

    out = stamp_expected_payment_dates(ap, _dpo_df(), AS_OF)

    # 2026-05-15 + 15 = 2026-05-30
    assert out.iloc[0]["expected_payment_date"] == dt.date(2026, 5, 30)
    assert out.iloc[0]["timing_method"] == METHOD_MASTER_FALLBACK
    assert out.iloc[0]["dpo_days_effective"] == 15


def test_no_balance_dpo_does_not_fall_back():
    """dpo_method 'no_balance' (dpo_days=0) is kept, not turned into a fallback.

    dpo_days=0 means pay immediately; the overdue clamp lifts it to as_of. We
    only fall back to dueDays when the vendor is ABSENT from the DPO table.
    """
    dpo_df = pd.DataFrame({
        "vendorNumber": ["VEND-Z"],
        "dpo_days":     [0],
        "dpo_method":   ["no_balance"],
    })
    ap = _ap_row("VEND-Z", dt.date(2026, 5, 15), due_days=30)

    out = stamp_expected_payment_dates(ap, dpo_df, AS_OF)

    # dpo_days=0 used (NOT dueDays=30); raw date 5/15 is overdue -> clamps to as_of
    assert out.iloc[0]["dpo_days_effective"] == 0
    assert out.iloc[0]["timing_method"] == "no_balance"
    assert out.iloc[0]["expected_payment_date"] == AS_OF
    assert out.iloc[0]["was_overdue"] == True


def test_terms_fallback_dpo_flows_through_as_timing_method():
    """If the DPO row carries terms_fallback method, that flows through."""
    dpo_df = pd.DataFrame({
        "vendorNumber": ["VEND-X"],
        "dpo_days":     [30],
        "dpo_method":   ["terms_fallback"],
    })
    ap = _ap_row("VEND-X", dt.date(2026, 5, 15))

    out = stamp_expected_payment_dates(ap, dpo_df, AS_OF)

    assert out.iloc[0]["timing_method"] == "terms_fallback"


def test_disbursement_amount_flips_invoice_sign_to_positive():
    """AP Invoice amounts are negative; disbursement_amount = -amount (positive)."""
    ap = _ap_row("VEND-A", dt.date(2026, 5, 20), amount=-25_000.0)

    out = stamp_expected_payment_dates(ap, _dpo_df(), AS_OF)

    assert out.iloc[0]["disbursement_amount"] == pytest.approx(25_000.0)
    assert out.iloc[0]["amount"] == pytest.approx(-25_000.0)  # raw preserved
    assert out.iloc[0]["source_stream"] == STREAM_ESTIMATED


def test_multiple_rows_per_vendor_stamped_independently():
    ap = pd.DataFrame({
        "vendorNumber": ["VEND-A", "VEND-A", "VEND-B"],
        "postingDate":  [dt.date(2026, 5, 20), dt.date(2026, 1, 1), dt.date(2026, 5, 10)],
        "documentType": ["Invoice", "Invoice", "Invoice"],
        "amount":       [-1000.0, -2000.0, -3000.0],
        "currencyCode": ["USD"] * 3,
        "entryNumber":  [1, 2, 3],
        "dueDays":      [30, 30, 30],
        "dueDate":      [None, None, None],
    })

    out = stamp_expected_payment_dates(ap, _dpo_df(), AS_OF)

    # First A row: 5/20 + 14 = 6/3, not overdue
    assert out.iloc[0]["expected_payment_date"] == dt.date(2026, 6, 3)
    assert out.iloc[0]["was_overdue"] == False
    # Second A row: overdue, clamps
    assert out.iloc[1]["expected_payment_date"] == AS_OF
    assert out.iloc[1]["was_overdue"] == True
    # B row: 5/10 + 30 = 6/9, not overdue
    assert out.iloc[2]["expected_payment_date"] == dt.date(2026, 6, 9)
    assert out.iloc[2]["was_overdue"] == False


# ---- Stream B documentType filter: non-Invoice rows excluded ----


def _multi_doctype_ap():
    """An open AP table with all four document types for the same vendor.

    Mirrors bc_vendor_ledger_entries: open invoices coexist with the occasional
    open Payment (unapplied), open Credit Memo (unapplied), and rare Refund.
    Only Invoice should land in the disbursements forecast.
    """
    return pd.DataFrame({
        "vendorNumber": ["VEND-A"] * 4,
        "postingDate":  [dt.date(2026, 5, 1)] * 4,
        "documentType": ["Invoice", "Payment", "Credit Memo", "Refund"],
        "amount":       [-10_000.0, 3_000.0, 500.0, -200.0],
        "currencyCode": ["USD"] * 4,
        "entryNumber":  [1, 2, 3, 4],
        "dueDays":      [30] * 4,
        "dueDate":      [None] * 4,
    })


def test_stream_b_excludes_open_payments():
    """An unapplied open Payment is cash already out; not a future disbursement."""
    out = stamp_expected_payment_dates(_multi_doctype_ap(), _dpo_df(), AS_OF)

    assert len(out) == 1
    assert out.iloc[0]["documentType"] == "Invoice"
    assert out.iloc[0]["disbursement_amount"] == pytest.approx(10_000.0)


def test_stream_b_excludes_open_credit_memos():
    """An unapplied credit memo reduces what we owe but isn't a disbursement."""
    ap = pd.DataFrame({
        "vendorNumber": ["VEND-A"],
        "postingDate":  [dt.date(2026, 5, 1)],
        "documentType": ["Credit Memo"],
        "amount":       [500.0],
        "currencyCode": ["USD"],
        "entryNumber":  [1],
        "dueDays":      [30],
        "dueDate":      [None],
    })

    out = stamp_expected_payment_dates(ap, _dpo_df(), AS_OF)

    assert len(out) == 0


def test_stream_b_excludes_blank_doctype_rows():
    """Rare manual journal entries with blank documentType are not disbursements."""
    ap = pd.DataFrame({
        "vendorNumber": ["VEND-A"],
        "postingDate":  [dt.date(2026, 5, 1)],
        "documentType": [None],
        "amount":       [-1_000.0],
        "currencyCode": ["USD"],
        "entryNumber":  [1],
        "dueDays":      [30],
        "dueDate":      [None],
    })

    out = stamp_expected_payment_dates(ap, _dpo_df(), AS_OF)

    assert len(out) == 0


def test_stream_b_empty_input_returns_empty():
    """Empty ap_open_with_due_dates input -> empty output, no error."""
    ap = pd.DataFrame({
        "vendorNumber": pd.Series([], dtype="object"),
        "postingDate":  pd.Series([], dtype="object"),
        "documentType": pd.Series([], dtype="object"),
        "amount":       pd.Series([], dtype="float"),
        "dueDays":      pd.Series([], dtype="int"),
        "dueDate":      pd.Series([], dtype="object"),
    })

    out = stamp_expected_payment_dates(ap, _dpo_df(), AS_OF)

    assert len(out) == 0


# ===========================================================================
# Combined -- build_disbursements (both streams concatenated)
# ===========================================================================


def test_combined_concats_both_streams():
    """Both streams concat cleanly even though column sets differ slightly."""
    ap = _ap_row("VEND-A", dt.date(2026, 5, 20), amount=-1000.0)
    history = _history_payment("VEND-A", dt.date(2026, 6, 1), 9000.0)

    out = build_disbursements(ap, _dpo_df(), history, AS_OF)

    assert len(out) == 2
    assert set(out["source_stream"]) == {STREAM_SCHEDULED, STREAM_ESTIMATED}
    # Scheduled row has NaN in the transform-only dueDays column (column union).
    sched = out[out["source_stream"] == STREAM_SCHEDULED].iloc[0]
    assert pd.isna(sched["dueDays"])
    # Both disbursement amounts are positive.
    assert (out["disbursement_amount"] > 0).all()


def test_combined_handles_empty_scheduled_stream():
    """No future payments -> output is Stream B only, no error."""
    ap = _ap_row("VEND-A", dt.date(2026, 5, 20))
    history = _history_payment("VEND-A", dt.date(2026, 5, 1), 5000.0)  # past only

    out = build_disbursements(ap, _dpo_df(), history, AS_OF)

    assert len(out) == 1
    assert out.iloc[0]["source_stream"] == STREAM_ESTIMATED


def test_combined_handles_empty_open_ap():
    """Empty open AP -> output is Stream A only, no error."""
    ap = pd.DataFrame({
        "vendorNumber": pd.Series([], dtype="object"),
        "postingDate":  pd.Series([], dtype="object"),
        "documentType": pd.Series([], dtype="object"),
        "amount":       pd.Series([], dtype="float"),
        "dueDays":      pd.Series([], dtype="int"),
        "dueDate":      pd.Series([], dtype="object"),
    })
    history = _history_payment("VEND-A", dt.date(2026, 6, 1), 9000.0)

    out = build_disbursements(ap, _dpo_df(), history, AS_OF)

    assert len(out) == 1
    assert out.iloc[0]["source_stream"] == STREAM_SCHEDULED


def test_combined_disbursement_amounts_all_positive():
    """Across both streams, disbursement_amount aggregates as positive dollars."""
    ap = pd.DataFrame({
        "vendorNumber": ["VEND-A", "VEND-B"],
        "postingDate":  [dt.date(2026, 5, 20), dt.date(2026, 5, 10)],
        "documentType": ["Invoice", "Invoice"],
        "amount":       [-10_000.0, -5_000.0],
        "currencyCode": ["USD", "USD"],
        "entryNumber":  [1, 2],
        "dueDays":      [30, 30],
        "dueDate":      [None, None],
    })
    history = _history_payment("VEND-A", dt.date(2026, 6, 1), 9000.0)

    out = build_disbursements(ap, _dpo_df(), history, AS_OF)

    # 10000 + 5000 (estimated) + 9000 (scheduled) = 24000
    assert out["disbursement_amount"].sum() == pytest.approx(24_000.0)
