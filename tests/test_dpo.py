"""Tests for src/calc/dpo.py."""
import datetime as dt

import pandas as pd
import pytest

from src.calc.dpo import (
    METHOD_NO_BALANCE,
    METHOD_RATIO,
    METHOD_TERMS_FALLBACK,
    PURCHASE_DOC_TYPES,
    WINDOW_DAYS,
    build_vendor_dpo,
    compute_dpo,
    trailing_net_purchases,
)


def test_compute_dpo_ratio_normal_case() -> None:
    dpo, method = compute_dpo(balance=1_000_000.0, trailing_purchases=12_000_000.0, terms_days=30)
    assert method == METHOD_RATIO
    assert dpo == 30


def test_compute_dpo_ratio_below_terms_indicates_fast_payer() -> None:
    """A vendor we pay much faster than terms allow (discount capture pattern)."""
    dpo, method = compute_dpo(balance=100_000.0, trailing_purchases=3_650_000.0, terms_days=30)
    assert method == METHOD_RATIO
    assert dpo == 10


def test_compute_dpo_ratio_above_window_returns_real_value() -> None:
    """Severely-aged AP produces DPO > window_days, surfaced rather than capped."""
    dpo, method = compute_dpo(balance=1_000_000.0, trailing_purchases=500_000.0, terms_days=30)
    assert method == METHOD_RATIO
    assert dpo == 730


def test_compute_dpo_zero_balance_returns_no_balance() -> None:
    dpo, method = compute_dpo(balance=0.0, trailing_purchases=12_000_000.0, terms_days=30)
    assert method == METHOD_NO_BALANCE
    assert dpo == 0


def test_compute_dpo_negative_balance_returns_no_balance() -> None:
    """Vendor with unapplied credits (we don't actually owe them) -> no_balance."""
    dpo, method = compute_dpo(balance=-50000.0, trailing_purchases=12_000_000.0, terms_days=30)
    assert method == METHOD_NO_BALANCE
    assert dpo == 0


def test_compute_dpo_nan_balance_returns_no_balance() -> None:
    dpo, method = compute_dpo(balance=float("nan"), trailing_purchases=12_000_000.0, terms_days=30)
    assert method == METHOD_NO_BALANCE
    assert dpo == 0


def test_compute_dpo_zero_purchases_falls_back_to_terms() -> None:
    dpo, method = compute_dpo(balance=5000.0, trailing_purchases=0.0, terms_days=30)
    assert method == METHOD_TERMS_FALLBACK
    assert dpo == 30


def test_compute_dpo_negative_purchases_falls_back_to_terms() -> None:
    """A vendor with net-negative purchases (more credit memos than invoices) is anomalous."""
    dpo, method = compute_dpo(balance=5000.0, trailing_purchases=-1000.0, terms_days=15)
    assert method == METHOD_TERMS_FALLBACK
    assert dpo == 15


def test_compute_dpo_fallback_carries_actual_terms_days() -> None:
    dpo, method = compute_dpo(balance=5000.0, trailing_purchases=0.0, terms_days=45)
    assert method == METHOD_TERMS_FALLBACK
    assert dpo == 45


def test_trailing_net_purchases_flips_sign_to_positive() -> None:
    """Invoice amounts are negative in AP; -sum() produces positive net purchases."""
    as_of = dt.date(2026, 5, 29)
    history = pd.DataFrame([
        {"vendorNumber": "1001", "documentType": "Invoice",
         "postingDate": dt.date(2026, 5, 1), "amount": -100000.0},
        {"vendorNumber": "1001", "documentType": "Invoice",
         "postingDate": dt.date(2026, 4, 1), "amount": -200000.0},
        {"vendorNumber": "1001", "documentType": "Credit Memo",
         "postingDate": dt.date(2026, 5, 15), "amount": 5000.0},
    ])

    result = trailing_net_purchases(history, as_of=as_of)

    assert result["1001"] == pytest.approx(295000.0)


def test_trailing_net_purchases_excludes_payments_and_refunds() -> None:
    """Only Invoice + Credit Memo count; Payments and Refunds are cash movements."""
    as_of = dt.date(2026, 5, 29)
    history = pd.DataFrame([
        {"vendorNumber": "1001", "documentType": "Invoice",
         "postingDate": dt.date(2026, 5, 1), "amount": -100000.0},
        {"vendorNumber": "1001", "documentType": "Payment",
         "postingDate": dt.date(2026, 5, 10), "amount": 50000.0},
        {"vendorNumber": "1001", "documentType": "Refund",
         "postingDate": dt.date(2026, 5, 20), "amount": 1000.0},
    ])

    result = trailing_net_purchases(history, as_of=as_of)

    assert result["1001"] == pytest.approx(100000.0)


def test_trailing_net_purchases_excludes_future_dated_book_entries() -> None:
    """Future-dated reversing pairs (the 2028/2036 ILAT3602 pattern) must be excluded.

    Without the upper bound on postingDate, these would pollute the purchase basis.
    """
    as_of = dt.date(2026, 5, 29)
    history = pd.DataFrame([
        {"vendorNumber": "6524", "documentType": "Invoice",
         "postingDate": dt.date(2026, 3, 1), "amount": -50000.0},
        {"vendorNumber": "6524", "documentType": "Invoice",
         "postingDate": dt.date(2028, 10, 28), "amount": -2400.0},
        {"vendorNumber": "6524", "documentType": "Credit Memo",
         "postingDate": dt.date(2028, 10, 28), "amount": 2400.0},
    ])

    result = trailing_net_purchases(history, as_of=as_of)

    assert result["6524"] == pytest.approx(50000.0)


def test_trailing_net_purchases_excludes_old_entries_before_window() -> None:
    as_of = dt.date(2026, 5, 29)
    history = pd.DataFrame([
        {"vendorNumber": "1001", "documentType": "Invoice",
         "postingDate": dt.date(2025, 6, 15), "amount": -100000.0},
        {"vendorNumber": "1001", "documentType": "Invoice",
         "postingDate": dt.date(2024, 6, 15), "amount": -500000.0},
    ])

    result = trailing_net_purchases(history, as_of=as_of)

    assert result["1001"] == pytest.approx(100000.0)


def test_trailing_net_purchases_groups_by_vendor() -> None:
    as_of = dt.date(2026, 5, 29)
    history = pd.DataFrame([
        {"vendorNumber": "1001", "documentType": "Invoice",
         "postingDate": dt.date(2026, 5, 1), "amount": -100000.0},
        {"vendorNumber": "1002", "documentType": "Invoice",
         "postingDate": dt.date(2026, 5, 1), "amount": -75000.0},
    ])

    result = trailing_net_purchases(history, as_of=as_of)

    assert result["1001"] == pytest.approx(100000.0)
    assert result["1002"] == pytest.approx(75000.0)


def test_purchase_doc_types_matches_module_constant() -> None:
    assert PURCHASE_DOC_TYPES == ("Invoice", "Credit Memo")


def test_window_days_constant() -> None:
    assert WINDOW_DAYS == 365


def test_build_vendor_dpo_resolves_all_paths() -> None:
    """The three fallback paths exercised in a single integration test."""
    as_of = dt.date(2026, 5, 29)
    vendors = pd.DataFrame([
        {"number": "1001", "balance": 7_215_282.0, "paymentTermsId": "terms-net30"},
        {"number": "1080", "balance": 0.0, "paymentTermsId": "terms-net30"},
        {"number": "9999", "balance": 5000.0, "paymentTermsId": "terms-net15"},
        {"number": "5686", "balance": -73516.0, "paymentTermsId": "terms-net30"},
    ])
    history = pd.DataFrame([
        {"vendorNumber": "1001", "documentType": "Invoice",
         "postingDate": dt.date(2026, 4, 1), "amount": -80_000_000.0},
        {"vendorNumber": "1080", "documentType": "Invoice",
         "postingDate": dt.date(2026, 4, 1), "amount": -1_000_000.0},
        {"vendorNumber": "5686", "documentType": "Invoice",
         "postingDate": dt.date(2026, 4, 1), "amount": -500_000.0},
    ])
    terms = pd.DataFrame([
        {"id": "terms-net30", "dueDateCalculation": "30D"},
        {"id": "terms-net15", "dueDateCalculation": "15D"},
    ])

    result = build_vendor_dpo(vendors, history, terms, as_of=as_of)
    by_vendor = {r["vendorNumber"]: r for _, r in result.iterrows()}

    assert by_vendor["1001"]["dpo_method"] == METHOD_RATIO
    assert by_vendor["1001"]["dpo_days"] == 33

    assert by_vendor["1080"]["dpo_method"] == METHOD_NO_BALANCE
    assert by_vendor["1080"]["dpo_days"] == 0

    assert by_vendor["9999"]["dpo_method"] == METHOD_TERMS_FALLBACK
    assert by_vendor["9999"]["dpo_days"] == 15

    assert by_vendor["5686"]["dpo_method"] == METHOD_NO_BALANCE
    assert by_vendor["5686"]["dpo_days"] == 0


def test_build_vendor_dpo_excludes_payments_from_purchase_basis() -> None:
    """Payments don't count as purchases."""
    as_of = dt.date(2026, 5, 29)
    vendors = pd.DataFrame([
        {"number": "1001", "balance": 100_000.0, "paymentTermsId": "terms-net30"},
    ])
    history = pd.DataFrame([
        {"vendorNumber": "1001", "documentType": "Invoice",
         "postingDate": dt.date(2026, 4, 1), "amount": -1_000_000.0},
        {"vendorNumber": "1001", "documentType": "Payment",
         "postingDate": dt.date(2026, 4, 15), "amount": 500_000.0},
    ])
    terms = pd.DataFrame([{"id": "terms-net30", "dueDateCalculation": "30D"}])

    result = build_vendor_dpo(vendors, history, terms, as_of=as_of)
    row = result[result["vendorNumber"] == "1001"].iloc[0]

    assert row["trailing_net_purchases"] == pytest.approx(1_000_000.0)
    # DPO = 100k / 1M * 365 = 36.5 -> banker's rounding -> 36
    assert row["dpo_days"] == 36