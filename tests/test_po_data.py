"""Tests for src/extract/po_data.py."""
import datetime as dt
import logging
import os

import pandas as pd
import pytest

from src.extract import po_data
from src.extract.po_data import (
    HEADERS_TABLE,
    LINES_TABLE,
    find_workbook,
    read_po_headers,
    read_po_lines,
    run,
)


def _headers(vendor_numbers=(1001, 1002)):
    return pd.DataFrame({
        "poNumber": ["PO1", "PO2"],
        "vendorNumber": list(vendor_numbers),   # numeric, to exercise coercion
        "vendorName": ["Vendor Alpha", "Vendor Beta"],
        "documentDate": [dt.date(2026, 5, 1), dt.date(2026, 5, 2)],
        "Status": ["Open", "Released"],
    })


def _lines(types=("Item", "Charge (Item)", "G/L Account", "Resource", "Item")):
    n = len(types)
    return pd.DataFrame({
        "expectedReceiptDate": [dt.date(2026, 6, 1)] * n,
        "quantityInvoiced": [0.0] * n,
        "Qty_to_Invoice": [0.0] * n,
        "quantityReceived": [0.0] * n,
        "Qty_to_Receive": [5] * n,
        "Line_Amount": [500.0] * n,
        "unitCost": [100.0] * n,
        "quantity": [5.0] * n,
        "description": [f"item {i}" for i in range(n)],
        "itemNumber": [f"I{i}" for i in range(n)],
        "Type": list(types),
        "lineNumber": [10000 + i for i in range(n)],
        "poNumber": ["PO1", "PO1", "PO2", "PO2", "PO2"][:n],
        "outstandingQuantity": [5] * n,
        "rbniQuantity": [0] * n,
        "rbniAmount": [0.0] * n,
        "outstandingAmount": [500.0] * n,
    })


def _write_workbook(path, headers=None, lines=None):
    headers = _headers() if headers is None else headers
    lines = _lines() if lines is None else lines
    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        headers.to_excel(xw, sheet_name="PurchaseOrders", index=False)
        lines.to_excel(xw, sheet_name="PurchaseOrderLines", index=False)
    return path


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    import src.db as db
    monkeypatch.setattr(db, "SQLITE_PATH", tmp_path / "test.db")
    return tmp_path / "test.db"


def test_happy_path_both_sheets_load(tmp_path):
    wb = _write_workbook(tmp_path / "po.xlsx")
    headers = read_po_headers(wb)
    lines = read_po_lines(wb)

    assert len(headers) == 2
    assert len(lines) == 5
    assert list(headers.columns) == ["poNumber", "vendorNumber", "vendorName", "documentDate", "Status"]
    assert "Type" in lines.columns


def test_vendor_number_coerced_to_clean_string(tmp_path):
    """Numeric vendorNumber (1001.0) becomes a clean string '1001' for the DPO join."""
    wb = _write_workbook(tmp_path / "po.xlsx")
    headers = read_po_headers(wb)

    assert headers["vendorNumber"].tolist() == ["1001", "1002"]
    assert pd.api.types.is_string_dtype(headers["vendorNumber"])


def test_expected_receipt_date_is_date(tmp_path):
    wb = _write_workbook(tmp_path / "po.xlsx")
    lines = read_po_lines(wb)
    v = lines["expectedReceiptDate"].iloc[0]
    assert isinstance(v, dt.date) and not isinstance(v, dt.datetime)


def test_mixed_type_values_pass_through(tmp_path):
    """Multiple Type values are retained (no uniformity assertion)."""
    wb = _write_workbook(tmp_path / "po.xlsx")
    lines = read_po_lines(wb)
    assert set(lines["Type"]) == {"Item", "Charge (Item)", "G/L Account", "Resource"}


def test_missing_column_raises_loudly(tmp_path):
    bad = pd.DataFrame({"poNumber": ["PO1"], "vendorNumber": [1]})  # missing cols
    wb = _write_workbook(tmp_path / "po.xlsx", headers=bad)
    with pytest.raises(ValueError, match="missing expected columns"):
        read_po_headers(wb)


def test_find_workbook_none_when_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(po_data, "ONEDRIVE_DATA_PATH", tmp_path)  # empty dir
    assert find_workbook() is None


def test_run_missing_file_warns_writes_empty_no_exception(tmp_db, tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(po_data, "ONEDRIVE_DATA_PATH", tmp_path / "nope")
    with caplog.at_level(logging.WARNING):
        run()  # must not raise
    assert any("not found" in r.message for r in caplog.records)
    from src.db import get_connection
    conn = get_connection()
    assert conn.execute(f"SELECT COUNT(*) FROM {HEADERS_TABLE}").fetchone()[0] == 0
    assert conn.execute(f"SELECT COUNT(*) FROM {LINES_TABLE}").fetchone()[0] == 0
    conn.close()


def test_run_stale_file_warns_but_continues(tmp_db, tmp_path, monkeypatch, caplog):
    folder = tmp_path / "od"
    folder.mkdir()
    wb = _write_workbook(folder / "BC_PurchaseOrderData.xlsx")
    old = (dt.datetime.now() - dt.timedelta(hours=30)).timestamp()
    os.utime(wb, (old, old))
    monkeypatch.setattr(po_data, "ONEDRIVE_DATA_PATH", folder)

    with caplog.at_level(logging.WARNING):
        run()  # must not raise
    assert any("stale" in r.message for r in caplog.records)
    from src.db import get_connection
    conn = get_connection()
    assert conn.execute(f"SELECT COUNT(*) FROM {LINES_TABLE}").fetchone()[0] == 5
    conn.close()
