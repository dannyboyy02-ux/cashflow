"""Tests for src/calc/so_receipts_timing.py -- the 3-tier collection-lag waterfall."""
import datetime as dt

import pandas as pd
import pytest

from src.calc.so_receipts_timing import (
    SO_METHOD_DEFAULT,
    SO_METHOD_DSO_RATIO,
    SO_METHOD_TERMS,
    SOURCE_STREAM,
    stamp_expected_collection_dates,
)
from src.transform.due_dates import DEFAULT_DUE_DAYS


AS_OF = dt.date(2026, 5, 29)


def _dso_df():
    """bc_customer_dso shape with the columns the waterfall reads.

    - CUST-RATIO  : Tier 1 (ratio)        -> lag = dso_days (40)
    - CUST-NOBAL  : Tier 2 (no_balance)   -> lag = terms_days (15)
    - CUST-TERMS  : Tier 2 (terms_fallback) -> lag = terms_days (45)
    - CUST-CARD   : Tier 2 (no_balance)   -> lag = terms_days (10, != default 30)
    (an absent customer exercises Tier 3.)
    """
    return pd.DataFrame({
        "customerNumber": ["CUST-RATIO", "CUST-NOBAL", "CUST-TERMS", "CUST-CARD"],
        "balance_due":    [50_000.0, 0.0, 0.0, 0.0],
        "trailing_net_sales": [450_000.0, 0.0, 0.0, 0.0],
        "terms_days":     [30, 15, 45, 10],
        "dso_days":       [40, 0, 30, 0],
        "dso_method":     ["ratio", "no_balance", "terms_fallback", "no_balance"],
    })


def _so_row(customer, planned_date, amount=1000.0):
    """One open-SO line matching so_open_with_expected_invoice_date shape.

    expected_invoice_date == plannedShipmentDate (set equal upstream in so_revenue).
    """
    return pd.DataFrame({
        "soNumber": ["SO1"],
        "lineNumber": ["10000"],
        "itemNumber": ["I1"],
        "description": ["widget"],
        "customerNumber": [customer],
        "customerName": ["Acme"],
        "quantity": [10.0],
        "quantityShipped": [0.0],
        "outstandingQuantity": [10.0],
        "unitPrice": [amount / 10.0],
        "outstandingAmount": [amount],
        "plannedShipmentDate": [planned_date],
        "expected_invoice_date": [planned_date],
    })


# ---- Tier 1: ratio customer -------------------------------------------------


def test_tier1_ratio_customer_uses_dso_days():
    # CUST-RATIO dso_days=40, ship 5/20 -> 6/29
    so = _so_row("CUST-RATIO", dt.date(2026, 5, 20))

    out = stamp_expected_collection_dates(so, _dso_df(), AS_OF)
    row = out.iloc[0]

    assert row["so_timing_method"] == SO_METHOD_DSO_RATIO
    assert row["dso_days_effective"] == 40
    assert row["expected_collection_date"] == dt.date(2026, 6, 29)
    assert row["was_overdue"] == False


# ---- Tier 2: no payment history -> card terms (the dominant case) -----------


def test_tier2_no_balance_customer_uses_terms_days():
    # CUST-NOBAL terms_days=15, ship 5/20 -> 6/4 (NOT zero-lag ship date)
    so = _so_row("CUST-NOBAL", dt.date(2026, 5, 20))

    out = stamp_expected_collection_dates(so, _dso_df(), AS_OF)
    row = out.iloc[0]

    assert row["so_timing_method"] == SO_METHOD_TERMS
    assert row["dso_days_effective"] == 15
    assert row["expected_collection_date"] == dt.date(2026, 6, 4)


def test_tier2_terms_fallback_customer_uses_terms_days():
    # CUST-TERMS terms_days=45, ship 5/20 -> 7/4
    so = _so_row("CUST-TERMS", dt.date(2026, 5, 20))

    out = stamp_expected_collection_dates(so, _dso_df(), AS_OF)
    row = out.iloc[0]

    assert row["so_timing_method"] == SO_METHOD_TERMS
    assert row["dso_days_effective"] == 45
    assert row["expected_collection_date"] == dt.date(2026, 7, 4)


def test_tier2_reads_card_terms_not_default():
    """A Tier-2 customer with terms_days != DEFAULT proves we use the card, not 30."""
    # CUST-CARD terms_days=10 (!= DEFAULT_DUE_DAYS 30), ship 5/20 -> 5/30
    so = _so_row("CUST-CARD", dt.date(2026, 5, 20))

    out = stamp_expected_collection_dates(so, _dso_df(), AS_OF)
    row = out.iloc[0]

    assert row["dso_days_effective"] == 10
    assert row["dso_days_effective"] != DEFAULT_DUE_DAYS
    assert row["so_timing_method"] == SO_METHOD_TERMS
    assert row["expected_collection_date"] == dt.date(2026, 5, 30)


# ---- Tier 3: customer absent from DSO -> house default ----------------------


def test_tier3_absent_customer_uses_default_due_days():
    # MYSTERY not in DSO -> lag = DEFAULT_DUE_DAYS (30), ship 5/20 -> 6/19
    so = _so_row("MYSTERY", dt.date(2026, 5, 20))

    out = stamp_expected_collection_dates(so, _dso_df(), AS_OF)
    row = out.iloc[0]

    assert row["so_timing_method"] == SO_METHOD_DEFAULT
    assert row["dso_days_effective"] == DEFAULT_DUE_DAYS  # 30
    assert row["expected_collection_date"] == dt.date(2026, 6, 19)


# ---- Overdue clamp (mirror receipts_timing) ---------------------------------


def test_overdue_so_line_clamps_to_as_of():
    # CUST-RATIO dso_days=40, ship 1/1 -> raw 2/10 < as_of -> clamp to 5/29
    so = _so_row("CUST-RATIO", dt.date(2026, 1, 1))

    out = stamp_expected_collection_dates(so, _dso_df(), AS_OF)
    row = out.iloc[0]

    assert row["expected_collection_date"] == AS_OF
    assert row["was_overdue"] == True
    assert row["days_overdue"] == 108  # 5/29 - 2/10


def test_tier2_overdue_also_clamps():
    """An old-ship Tier-2 line whose ship+terms is still past clamps to as_of."""
    # CUST-NOBAL terms_days=15, ship 1/1 -> 1/16 < as_of -> clamp
    so = _so_row("CUST-NOBAL", dt.date(2026, 1, 1))

    out = stamp_expected_collection_dates(so, _dso_df(), AS_OF)
    row = out.iloc[0]

    assert row["expected_collection_date"] == AS_OF
    assert row["was_overdue"] == True


# ---- Tags / value passthrough ------------------------------------------------


def test_source_stream_is_open_so():
    so = _so_row("CUST-RATIO", dt.date(2026, 5, 20))

    out = stamp_expected_collection_dates(so, _dso_df(), AS_OF)

    assert out.iloc[0]["source_stream"] == SOURCE_STREAM
    assert SOURCE_STREAM == "open_so"


def test_outstanding_amount_preserved_as_value_column():
    so = _so_row("CUST-RATIO", dt.date(2026, 5, 20), amount=2500.0)

    out = stamp_expected_collection_dates(so, _dso_df(), AS_OF)

    assert out.iloc[0]["outstandingAmount"] == pytest.approx(2500.0)
    assert "amount" not in out.columns  # SO side uses outstandingAmount


def test_mixed_tiers_resolved_independently():
    """One frame with all three tiers resolves each line by its own customer."""
    so = pd.concat([
        _so_row("CUST-RATIO", dt.date(2026, 5, 20)),   # tier 1 -> +40
        _so_row("CUST-NOBAL", dt.date(2026, 5, 20)),   # tier 2 -> +15
        _so_row("MYSTERY",    dt.date(2026, 5, 20)),   # tier 3 -> +30
    ], ignore_index=True)

    out = stamp_expected_collection_dates(so, _dso_df(), AS_OF)

    assert list(out["so_timing_method"]) == [
        SO_METHOD_DSO_RATIO, SO_METHOD_TERMS, SO_METHOD_DEFAULT,
    ]
    assert list(out["dso_days_effective"]) == [40, 15, 30]
