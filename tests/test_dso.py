"""Tests for src/calc/dso.py."""
import pandas as pd
import pytest

from src.calc.dso import (
    METHOD_NO_BALANCE,
    METHOD_RATIO,
    METHOD_TERMS_FALLBACK,
    SALES_DOC_TYPES,
    WINDOW_DAYS,
    build_customer_dso,
    compute_dso,
    trailing_net_sales,
)


# ---- compute_dso: the formula and the fallback chain --------------------------

def test_compute_dso_ratio_normal_case():
    # 10,000 AR / 120,000 sales * 365 = 30.42 -> rounded to 30
    days, method = compute_dso(balance=10_000.0, trailing_sales=120_000.0, terms_days=30)
    assert (days, method) == (30, METHOD_RATIO)


def test_compute_dso_ratio_above_window():
    # 50,000 AR / 5,000 sales * 365 = 3650; deliberately NOT capped --
    # the receipts layer decides how to treat outliers.
    days, method = compute_dso(balance=50_000.0, trailing_sales=5_000.0, terms_days=10)
    assert (days, method) == (3650, METHOD_RATIO)


def test_compute_dso_zero_balance_returns_no_balance():
    days, method = compute_dso(balance=0.0, trailing_sales=120_000.0, terms_days=30)
    assert (days, method) == (0, METHOD_NO_BALANCE)


def test_compute_dso_negative_balance_returns_no_balance():
    """Customers with credit balances (overpayment, unapplied credits) get DSO=0."""
    days, method = compute_dso(balance=-1_000.0, trailing_sales=120_000.0, terms_days=30)
    assert (days, method) == (0, METHOD_NO_BALANCE)


def test_compute_dso_nan_balance_returns_no_balance():
    days, method = compute_dso(balance=pd.NA, trailing_sales=120_000.0, terms_days=30)
    assert (days, method) == (0, METHOD_NO_BALANCE)


def test_compute_dso_zero_sales_falls_back_to_terms():
    days, method = compute_dso(balance=3_000.0, trailing_sales=0.0, terms_days=30)
    assert (days, method) == (30, METHOD_TERMS_FALLBACK)


def test_compute_dso_negative_sales_falls_back_to_terms():
    """Heavy credit memos exceeding invoices over the window."""
    days, method = compute_dso(balance=1_000.0, trailing_sales=-500.0, terms_days=15)
    assert (days, method) == (15, METHOD_TERMS_FALLBACK)


def test_compute_dso_fallback_carries_actual_terms_days():
    """ON RECPT customers (1-day terms) should fall back to 1, not the default 30."""
    days, method = compute_dso(balance=2_000.0, trailing_sales=0.0, terms_days=1)
    assert (days, method) == (1, METHOD_TERMS_FALLBACK)


# ---- trailing_net_sales: signed aggregation excluding non-sales rows ---------

def _history_df(rows):
    cols = ["customerNumber", "documentType", "amount", "postingDate"]
    return pd.DataFrame(rows, columns=cols)


def test_trailing_net_sales_sums_invoice_and_credit_memo_with_signs():
    """Invoice positive + credit memo negative -> nets automatically."""
    df = _history_df([
        ("CUST-A", "Invoice", 10_000.0, "2026-01-15"),
        ("CUST-A", "Credit Memo", -1_500.0, "2026-02-10"),
        ("CUST-A", "Invoice", 5_000.0, "2026-03-20"),
    ])

    result = trailing_net_sales(df)

    assert result["CUST-A"] == pytest.approx(13_500.0)


def test_trailing_net_sales_excludes_payments_refunds_and_blanks():
    """Only Invoice and Credit Memo count toward sales."""
    df = _history_df([
        ("CUST-A", "Invoice", 10_000.0, "2026-01-15"),
        ("CUST-A", "Payment", -10_000.0, "2026-02-15"),
        ("CUST-A", "Refund", 100.0, "2026-03-15"),
        ("CUST-A", None, 50.0, "2026-04-15"),
    ])

    result = trailing_net_sales(df)

    assert result["CUST-A"] == pytest.approx(10_000.0)


def test_trailing_net_sales_groups_by_customer():
    df = _history_df([
        ("CUST-A", "Invoice", 1_000.0, "2026-01-15"),
        ("CUST-B", "Invoice", 2_000.0, "2026-01-15"),
        ("CUST-A", "Invoice", 500.0, "2026-02-15"),
    ])

    result = trailing_net_sales(df)

    assert result["CUST-A"] == pytest.approx(1_500.0)
    assert result["CUST-B"] == pytest.approx(2_000.0)


def test_sales_doc_types_matches_module_constant():
    """Sanity check that the constant the calc reads from matches docs."""
    assert set(SALES_DOC_TYPES) == {"Invoice", "Credit Memo"}


# ---- build_customer_dso: full table with all four paths -----------------------

def _customers_df():
    return pd.DataFrame({
        "id": list("abcdef"),
        "number": ["CUST-A","CUST-B","CUST-C","CUST-D","CUST-E","CUST-F"],
        "paymentTermsId": [
            "TERM-NET30","TERM-NET30","TERM-NET10",
            "TERM-NET30","TERM-NET30",
            "00000000-0000-0000-0000-000000000000",  # blank -> fallback default
        ],
        "balanceDue":  [10_000.0, 50_000.0, 50_000.0, 0.0, 3_000.0, 2_000.0],
    })


def _terms_df():
    return pd.DataFrame({
        "id": ["TERM-NET30", "TERM-NET10"],
        "dueDateCalculation": ["30D", "10D"],
    })


def _history_for_build():
    """Sales rows: A 120k/yr, B 300k/yr, C 5k/yr. D/E/F have no history."""
    rows = []
    for cust, total in [("CUST-A", 120_000.0), ("CUST-B", 300_000.0), ("CUST-C", 5_000.0)]:
        for i in range(12):
            rows.append((cust, "Invoice", total / 12, f"2025-{(i % 12) + 1:02d}-15"))
    # one payment that must be excluded from sales aggregation
    rows.append(("CUST-A", "Payment", -50_000.0, "2026-03-01"))
    return _history_df(rows)


def test_build_customer_dso_resolves_all_paths():
    df = build_customer_dso(_customers_df(), _history_for_build(), _terms_df())

    by_cust = df.set_index("customerNumber")

    assert by_cust.loc["CUST-A", "dso_method"] == METHOD_RATIO
    assert by_cust.loc["CUST-A", "dso_days"] == 30
    assert by_cust.loc["CUST-B", "dso_method"] == METHOD_RATIO
    assert by_cust.loc["CUST-B", "dso_days"] == 61
    assert by_cust.loc["CUST-C", "dso_method"] == METHOD_RATIO
    assert by_cust.loc["CUST-C", "dso_days"] == 3650
    assert by_cust.loc["CUST-D", "dso_method"] == METHOD_NO_BALANCE
    assert by_cust.loc["CUST-D", "dso_days"] == 0
    assert by_cust.loc["CUST-E", "dso_method"] == METHOD_TERMS_FALLBACK
    assert by_cust.loc["CUST-E", "dso_days"] == 30
    assert by_cust.loc["CUST-F", "dso_method"] == METHOD_TERMS_FALLBACK
    assert by_cust.loc["CUST-F", "dso_days"] == 30


def test_build_customer_dso_excludes_payments_from_sales():
    """The Payment row for CUST-A must not deflate its sales total."""
    df = build_customer_dso(_customers_df(), _history_for_build(), _terms_df())

    a = df[df["customerNumber"] == "CUST-A"].iloc[0]
    # 12 monthly invoices of 10,000 = 120,000 exactly; payment excluded
    assert a["trailing_net_sales"] == pytest.approx(120_000.0)


def test_window_days_constant():
    assert WINDOW_DAYS == 365