"""Tests for src/transform/so_revenue.py."""
import datetime as dt
import logging

import pandas as pd
import pytest

from src.transform.so_revenue import OUTPUT_COLUMNS, build_so_revenue


def _headers(*rows):
    """Rows: (soNumber, customerNumber, customerName, status)."""
    return pd.DataFrame({
        "soNumber": [r[0] for r in rows],
        "customerNumber": [r[1] for r in rows],
        "customerName": [r[2] for r in rows],
        "status": [r[3] for r in rows],
    })


def _line(soNumber, type_="Item", outstanding_qty=10, unit_price=100.0,
          planned=dt.date(2026, 6, 1), line_no=10000):
    return {
        "soNumber": soNumber, "lineNumber": line_no, "Type": type_,
        "itemNumber": "I1", "description": "widget",
        "quantity": 10.0, "unitPrice": unit_price, "quantityShipped": 0.0,
        "plannedShipmentDate": planned,
        "outstandingQuantity": float(outstanding_qty),
        "outstandingAmount": float(outstanding_qty) * unit_price,
    }


def _lines(*dicts):
    return pd.DataFrame(list(dicts))


def test_inner_join_brings_customer_onto_each_line():
    headers = _headers(("SO1", "CUST-A", "Alpha", "Open"))
    lines = _lines(_line("SO1"))

    out = build_so_revenue(headers, lines)

    assert len(out) == 1
    assert out.iloc[0]["customerNumber"] == "CUST-A"
    assert out.iloc[0]["customerName"] == "Alpha"


def test_line_with_no_matching_header_is_dropped():
    headers = _headers(("SO1", "CUST-A", "Alpha", "Open"))
    lines = _lines(_line("SO-ORPHAN"))

    out = build_so_revenue(headers, lines)

    assert len(out) == 0


def test_expected_invoice_date_equals_planned_shipment_date():
    headers = _headers(("SO1", "CUST-A", "Alpha", "Open"))
    lines = _lines(_line("SO1", planned=dt.date(2026, 7, 15)))

    out = build_so_revenue(headers, lines)

    assert out.iloc[0]["expected_invoice_date"] == dt.date(2026, 7, 15)
    assert out.iloc[0]["expected_invoice_date"] == out.iloc[0]["plannedShipmentDate"]


def test_zero_outstanding_quantity_is_filtered_out():
    headers = _headers(("SO1", "CUST-A", "Alpha", "Open"))
    lines = _lines(
        _line("SO1", outstanding_qty=0, line_no=10000),
        _line("SO1", outstanding_qty=5, line_no=20000),
    )

    out = build_so_revenue(headers, lines)

    assert len(out) == 1
    assert out.iloc[0]["lineNumber"] == 20000


def test_outstanding_amount_equals_qty_times_price():
    """PQ-drift guard: recompute and assert outstandingAmount on the output."""
    headers = _headers(("SO1", "CUST-A", "Alpha", "Open"))
    lines = _lines(_line("SO1", outstanding_qty=15, unit_price=25.0))  # 375

    out = build_so_revenue(headers, lines)
    row = out.iloc[0]

    assert row["outstandingAmount"] == pytest.approx(row["outstandingQuantity"] * row["unitPrice"])
    assert row["outstandingAmount"] == pytest.approx(375.0)


def test_non_item_row_dropped_and_logs_error(caplog):
    headers = _headers(("SO1", "CUST-A", "Alpha", "Open"))
    lines = _lines(
        _line("SO1", type_="Item", line_no=10000),
        _line("SO1", type_="G/L Account", line_no=20000),
    )

    with caplog.at_level(logging.ERROR):
        out = build_so_revenue(headers, lines)

    assert len(out) == 1
    assert out.iloc[0]["lineNumber"] == 10000
    assert any("PQ filter regression" in r.message for r in caplog.records)


def test_output_has_no_type_column():
    headers = _headers(("SO1", "CUST-A", "Alpha", "Open"))
    lines = _lines(_line("SO1"))

    out = build_so_revenue(headers, lines)

    assert "Type" not in out.columns
    assert list(out.columns) == OUTPUT_COLUMNS
    assert "currencyCode" not in out.columns  # intentionally excluded upstream
