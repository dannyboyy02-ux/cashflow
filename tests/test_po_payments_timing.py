"""Tests for src/calc/po_payments_timing.py -- DPO waterfall + two sub-streams."""
import datetime as dt

import pandas as pd
import pytest

from src.calc.po_payments_timing import (
    INVOICE_LAG_DAYS,
    METHOD_DEFAULT,
    SOURCE_STREAM_OUTSTANDING,
    SOURCE_STREAM_RBNI,
    build_po_payments,
)
from src.transform.ap_due_dates import DEFAULT_DUE_DAYS


AS_OF = dt.date(2026, 5, 29)


def _dpo_df():
    """bc_vendor_dpo shape.

    - VEND-RATIO : Tier 1 (ratio)          -> lag = dpo_days (20)
    - VEND-NOBAL : Tier 2 (no_balance)     -> lag = terms_days (15)
    - VEND-TERMS : Tier 2 (terms_fallback) -> lag = terms_days (45)
    - VEND-CARD  : Tier 2 (no_balance)     -> lag = terms_days (10, != default 30)
    (an absent vendor exercises Tier 3.)
    """
    return pd.DataFrame({
        "vendorNumber": ["VEND-RATIO", "VEND-NOBAL", "VEND-TERMS", "VEND-CARD"],
        "balance": [100_000.0, 0.0, 0.0, 0.0],
        "trailing_net_purchases": [1_800_000.0, 0.0, 0.0, 0.0],
        "terms_days": [30, 15, 45, 10],
        "dpo_days": [20, 0, 30, 0],
        "dpo_method": ["ratio", "no_balance", "terms_fallback", "no_balance"],
    })


def _po_line(vendor, rbni_amount=0.0, outstanding_amount=0.0,
             expected_receipt=dt.date(2026, 6, 1), line_no=10000, type_="Item"):
    return {
        "poNumber": "PO1", "lineNumber": line_no, "vendorNumber": vendor,
        "vendorName": "Vendor X", "itemNumber": "I1", "description": "widget",
        "Type": type_,
        "expectedReceiptDate": expected_receipt,
        "rbniAmount": rbni_amount, "outstandingAmount": outstanding_amount,
    }


def _lines(*dicts):
    return pd.DataFrame(list(dicts))


# ---- Waterfall tiers (use an outstanding line so the lag drives the date) ----


def _outstanding_payment_date(vendor, lag_expected):
    """Helper: build one outstanding line for `vendor`, return its row."""
    line = _po_line(vendor, outstanding_amount=1000.0, expected_receipt=dt.date(2026, 6, 1))
    out = build_po_payments(_lines(line), _dpo_df(), AS_OF)
    row = out.iloc[0]
    # expected = receipt(6/1) + INVOICE_LAG + lag
    expected = dt.date(2026, 6, 1) + dt.timedelta(days=INVOICE_LAG_DAYS + lag_expected)
    assert row["expected_payment_date"] == expected
    return row


def test_tier1_ratio_vendor_uses_dpo_days():
    row = _outstanding_payment_date("VEND-RATIO", lag_expected=20)
    assert row["payment_method"] == "ratio"


def test_tier2_no_balance_vendor_uses_terms_days():
    row = _outstanding_payment_date("VEND-NOBAL", lag_expected=15)
    assert row["payment_method"] == "no_balance"


def test_tier2_terms_fallback_vendor_uses_terms_days():
    row = _outstanding_payment_date("VEND-TERMS", lag_expected=45)
    assert row["payment_method"] == "terms_fallback"


def test_tier3_absent_vendor_uses_default_due_days():
    line = _po_line("MYSTERY", outstanding_amount=1000.0, expected_receipt=dt.date(2026, 6, 1))
    out = build_po_payments(_lines(line), _dpo_df(), AS_OF)
    row = out.iloc[0]
    expected = dt.date(2026, 6, 1) + dt.timedelta(days=INVOICE_LAG_DAYS + DEFAULT_DUE_DAYS)
    assert row["expected_payment_date"] == expected
    assert row["payment_method"] == METHOD_DEFAULT


def test_tier2_reads_vendor_card_not_default():
    """VEND-CARD terms_days=10 (!= DEFAULT 30) proves Tier 2 reads the vendor card."""
    line = _po_line("VEND-CARD", outstanding_amount=1000.0, expected_receipt=dt.date(2026, 6, 1))
    out = build_po_payments(_lines(line), _dpo_df(), AS_OF)
    row = out.iloc[0]
    expected = dt.date(2026, 6, 1) + dt.timedelta(days=INVOICE_LAG_DAYS + 10)
    assert row["expected_payment_date"] == expected
    assert 10 != DEFAULT_DUE_DAYS


# ---- Sub-stream emission -----------------------------------------------------


def test_rbni_only_line_emits_one_rbni_row():
    line = _po_line("VEND-RATIO", rbni_amount=5000.0, outstanding_amount=0.0)
    out = build_po_payments(_lines(line), _dpo_df(), AS_OF)

    assert len(out) == 1
    row = out.iloc[0]
    assert row["source_stream"] == SOURCE_STREAM_RBNI
    assert row["amount"] == pytest.approx(5000.0)
    # RBNI: as_of + INVOICE_LAG + lag(ratio=20); never overdue
    assert row["expected_payment_date"] == AS_OF + dt.timedelta(days=INVOICE_LAG_DAYS + 20)
    assert row["was_overdue"] == False
    assert row["days_overdue"] == 0


def test_outstanding_only_line_emits_one_outstanding_row():
    line = _po_line("VEND-RATIO", rbni_amount=0.0, outstanding_amount=8000.0)
    out = build_po_payments(_lines(line), _dpo_df(), AS_OF)

    assert len(out) == 1
    assert out.iloc[0]["source_stream"] == SOURCE_STREAM_OUTSTANDING
    assert out.iloc[0]["amount"] == pytest.approx(8000.0)


def test_partial_receipt_line_emits_two_rows():
    """Both rbniAmount > 0 and outstandingAmount > 0 -> one row per sub-stream."""
    line = _po_line("VEND-RATIO", rbni_amount=3000.0, outstanding_amount=7000.0)
    out = build_po_payments(_lines(line), _dpo_df(), AS_OF)

    assert len(out) == 2
    assert set(out["source_stream"]) == {SOURCE_STREAM_RBNI, SOURCE_STREAM_OUTSTANDING}
    by_stream = out.set_index("source_stream")["amount"].to_dict()
    assert by_stream[SOURCE_STREAM_RBNI] == pytest.approx(3000.0)
    assert by_stream[SOURCE_STREAM_OUTSTANDING] == pytest.approx(7000.0)


def test_zero_amount_lines_emit_no_rows():
    line = _po_line("VEND-RATIO", rbni_amount=0.0, outstanding_amount=0.0)
    out = build_po_payments(_lines(line), _dpo_df(), AS_OF)
    assert len(out) == 0


# ---- Overdue clamp (outstanding only) ---------------------------------------


def test_outstanding_overdue_clamps_to_as_of():
    """A stale expectedReceiptDate makes raw payment date < as_of -> clamp."""
    # receipt 2026-01-01 + 7 + 20 = 2026-01-28 < as_of(5/29) -> clamp
    line = _po_line("VEND-RATIO", outstanding_amount=1000.0,
                    expected_receipt=dt.date(2026, 1, 1))
    out = build_po_payments(_lines(line), _dpo_df(), AS_OF)
    row = out.iloc[0]

    assert row["expected_payment_date"] == AS_OF
    assert row["was_overdue"] == True
    assert row["days_overdue"] > 0
    # raw = 2026-01-28 -> 121 days before 5/29
    assert row["days_overdue"] == (AS_OF - dt.date(2026, 1, 28)).days


def test_mixed_lines_resolve_independently():
    lines = _lines(
        _po_line("VEND-RATIO", rbni_amount=1000.0, line_no=10000),         # rbni only
        _po_line("VEND-NOBAL", outstanding_amount=2000.0, line_no=20000),  # outstanding only
        _po_line("VEND-TERMS", rbni_amount=500.0, outstanding_amount=900.0, line_no=30000),  # both
    )
    out = build_po_payments(lines, _dpo_df(), AS_OF)
    # 1 + 1 + 2 = 4 rows
    assert len(out) == 4
    assert (out["source_stream"] == SOURCE_STREAM_RBNI).sum() == 2
    assert (out["source_stream"] == SOURCE_STREAM_OUTSTANDING).sum() == 2


def test_empty_input_returns_empty_with_columns():
    empty = pd.DataFrame(columns=[
        "poNumber", "lineNumber", "vendorNumber", "vendorName", "itemNumber",
        "description", "Type", "expectedReceiptDate", "rbniAmount", "outstandingAmount",
    ])
    out = build_po_payments(empty, _dpo_df(), AS_OF)
    assert len(out) == 0
    assert "source_stream" in out.columns


# ---- Tier-3 outstanding haircut (FP&A conversion certainty) ------------------


def test_outstanding_haircut_scales_only_outstanding():
    """A 30% haircut times the PO-outstanding amount by 0.70; RBNI is untouched."""
    line = _po_line("VEND-RATIO", rbni_amount=1000.0, outstanding_amount=2000.0)
    out = build_po_payments(_lines(line), _dpo_df(), AS_OF, outstanding_haircut=0.30)
    by = out.set_index("source_stream")["amount"].to_dict()
    assert by[SOURCE_STREAM_RBNI] == pytest.approx(1000.0)        # RBNI unchanged
    assert by[SOURCE_STREAM_OUTSTANDING] == pytest.approx(1400.0)  # 2000 * 0.70


def test_outstanding_haircut_zero_is_gross_view():
    line = _po_line("VEND-RATIO", outstanding_amount=2000.0)
    out = build_po_payments(_lines(line), _dpo_df(), AS_OF, outstanding_haircut=0.0)
    assert out.iloc[0]["amount"] == pytest.approx(2000.0)


def test_load_outstanding_haircut_missing_file_defaults_zero(tmp_path):
    from src.calc.po_payments_timing import load_outstanding_haircut
    assert load_outstanding_haircut(tmp_path / "nope.json") == 0.0


def test_load_outstanding_haircut_reads_value(tmp_path):
    import json
    from src.calc.po_payments_timing import load_outstanding_haircut
    p = tmp_path / "po_config.json"
    p.write_text(json.dumps({"outstanding_haircut_pct": 0.25}))
    assert load_outstanding_haircut(p) == pytest.approx(0.25)
