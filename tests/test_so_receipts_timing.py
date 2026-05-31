"""Tests for src/calc/so_receipts_timing.py (mirror of test_receipts_timing.py)."""
import datetime as dt

import pandas as pd
import pytest

from src.calc.so_receipts_timing import (
    METHOD_MASTER_FALLBACK,
    SOURCE_STREAM,
    stamp_expected_collection_dates,
)
from src.transform.due_dates import DEFAULT_DUE_DAYS


AS_OF = dt.date(2026, 5, 29)


def _dso_df():
    return pd.DataFrame({
        "customerNumber": ["CUST-A", "CUST-B"],
        "dso_days":       [40, 45],
        "dso_method":     ["ratio", "terms_fallback"],
    })


def _so_row(customer, invoice_date, amount=1000.0):
    """One open-SO line matching so_open_with_expected_invoice_date shape."""
    return pd.DataFrame({
        "soNumber": ["SO1"],
        "lineNumber": ["10000"],
        "itemNumber": ["I1"],
        "description": ["widget"],
        "customerNumber": [customer],
        "customerName": ["Alpha"],
        "quantity": [10.0],
        "quantityShipped": [0.0],
        "outstandingQuantity": [10.0],
        "unitPrice": [amount / 10.0],
        "outstandingAmount": [amount],
        "plannedShipmentDate": [invoice_date],
        "expected_invoice_date": [invoice_date],
    })


def test_future_so_line_not_overdue():
    """expected_invoice_date + dso_days > as_of -> natural future timing."""
    # CUST-A DSO=40, invoice 5/20 -> 6/29
    so = _so_row("CUST-A", dt.date(2026, 5, 20))

    out = stamp_expected_collection_dates(so, _dso_df(), AS_OF)

    assert out.iloc[0]["expected_collection_date"] == dt.date(2026, 6, 29)
    assert out.iloc[0]["was_overdue"] == False
    assert out.iloc[0]["days_overdue"] == 0
    assert out.iloc[0]["timing_method"] == "ratio"


def test_overdue_so_line_clamps_to_as_of():
    """expected_invoice_date + dso_days < as_of -> clamp to as_of, tag overdue."""
    # CUST-A DSO=40, invoice 1/1 -> would be 2/10; clamps to 5/29
    so = _so_row("CUST-A", dt.date(2026, 1, 1))

    out = stamp_expected_collection_dates(so, _dso_df(), AS_OF)

    assert out.iloc[0]["expected_collection_date"] == AS_OF
    assert out.iloc[0]["was_overdue"] == True
    assert out.iloc[0]["days_overdue"] == 108  # 5/29 - 2/10


def test_customer_missing_from_dso_falls_back_to_default_due_days():
    so = _so_row("MYSTERY", dt.date(2026, 5, 20))

    out = stamp_expected_collection_dates(so, _dso_df(), AS_OF)

    assert out.iloc[0]["dso_days_effective"] == DEFAULT_DUE_DAYS  # 30
    assert out.iloc[0]["timing_method"] == METHOD_MASTER_FALLBACK
    # 5/20 + 30 = 6/19
    assert out.iloc[0]["expected_collection_date"] == dt.date(2026, 6, 19)


def test_source_stream_is_open_so():
    so = _so_row("CUST-A", dt.date(2026, 5, 20))

    out = stamp_expected_collection_dates(so, _dso_df(), AS_OF)

    assert out.iloc[0]["source_stream"] == SOURCE_STREAM
    assert SOURCE_STREAM == "open_so"


def test_outstanding_amount_preserved_as_value_column():
    so = _so_row("CUST-A", dt.date(2026, 5, 20), amount=2500.0)

    out = stamp_expected_collection_dates(so, _dso_df(), AS_OF)

    assert out.iloc[0]["outstandingAmount"] == pytest.approx(2500.0)
    assert "amount" not in out.columns  # SO side uses outstandingAmount, not amount


def test_terms_fallback_method_flows_through():
    so = _so_row("CUST-B", dt.date(2026, 5, 20))  # CUST-B method = terms_fallback

    out = stamp_expected_collection_dates(so, _dso_df(), AS_OF)

    assert out.iloc[0]["timing_method"] == "terms_fallback"
