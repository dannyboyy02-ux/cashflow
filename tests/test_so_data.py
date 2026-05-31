"""Tests for src/extract/so_data.py."""
import datetime as dt
import logging
import os

import pandas as pd
import pytest

from src.extract import so_data
from src.extract.so_data import (
    HEADERS_TABLE,
    LINES_TABLE,
    find_workbook,
    read_so_headers,
    read_so_lines,
    run,
)


def _write_workbook(path, headers=None, lines=None):
    """Write a temp BC_SalesOrderData.xlsx with both sheets."""
    if headers is None:
        headers = pd.DataFrame({
            "soNumber": ["SO1", "SO2", "SO3"],
            "customerNumber": ["CUST-A", "CUST-B", "CUST-C"],
            "customerName": ["Alpha", "Beta", "Gamma"],
            "status": ["Open", "Released", "Open"],
        })
    if lines is None:
        lines = pd.DataFrame({
            "soNumber": ["SO1", "SO1", "SO2", "SO3", "SO3"],
            "lineNumber": [10000, 20000, 10000, 10000, 20000],
            "Type": ["Item"] * 5,
            "itemNumber": ["I1", "I2", "I3", "I4", "I5"],
            "description": ["a", "b", "c", "d", "e"],
            "quantity": [10, 5, 20, 1, 2],
            "unitPrice": [100.0, 50.0, 25.0, 1000.0, 500.0],
            "quantityShipped": [0, 0, 5, 0, 1],
            "plannedShipmentDate": [
                dt.date(2026, 6, 1), dt.date(2026, 6, 8), dt.date(2026, 7, 1),
                dt.date(2026, 8, 1), dt.date(2026, 8, 15),
            ],
            "outstandingQuantity": [10, 5, 15, 1, 1],
            "outstandingAmount": [1000.0, 250.0, 375.0, 1000.0, 500.0],
        })
    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        headers.to_excel(xw, sheet_name="SalesOrders", index=False)
        lines.to_excel(xw, sheet_name="SalesOrderLines", index=False)
    return path


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    import src.db as db
    monkeypatch.setattr(db, "SQLITE_PATH", tmp_path / "test.db")
    return tmp_path / "test.db"


def test_read_headers_three_rows_four_cols_correct_dtypes(tmp_path):
    wb = _write_workbook(tmp_path / "wb.xlsx")
    df = read_so_headers(wb)

    assert len(df) == 3
    # 4 columns -- currencyCode intentionally excluded upstream.
    assert list(df.columns) == ["soNumber", "customerNumber", "customerName", "status"]
    assert pd.api.types.is_string_dtype(df["soNumber"])
    assert pd.api.types.is_string_dtype(df["status"])


def test_read_lines_five_rows_all_cols_including_Type(tmp_path):
    wb = _write_workbook(tmp_path / "wb.xlsx")
    df = read_so_lines(wb)

    assert len(df) == 5
    assert "Type" in df.columns  # capital-T preserved
    assert list(df.columns) == [
        "soNumber", "lineNumber", "Type", "itemNumber", "description",
        "quantity", "unitPrice", "quantityShipped", "plannedShipmentDate",
        "outstandingQuantity", "outstandingAmount",
    ]


def test_planned_shipment_date_is_date_not_datetime(tmp_path):
    wb = _write_workbook(tmp_path / "wb.xlsx")
    df = read_so_lines(wb)
    v = df["plannedShipmentDate"].iloc[0]
    assert isinstance(v, dt.date) and not isinstance(v, dt.datetime)


def test_outstanding_columns_are_numeric(tmp_path):
    wb = _write_workbook(tmp_path / "wb.xlsx")
    df = read_so_lines(wb)
    assert pd.api.types.is_numeric_dtype(df["outstandingQuantity"])
    assert pd.api.types.is_numeric_dtype(df["outstandingAmount"])


def test_type_column_is_uniformly_item(tmp_path):
    """A failure here means the PQ Item-filter regressed; surface it loudly."""
    wb = _write_workbook(tmp_path / "wb.xlsx")
    df = read_so_lines(wb)
    assert df["Type"].unique().tolist() == ["Item"]


def test_missing_column_raises_loudly(tmp_path):
    """Column-name drift (PQ rename regression) raises rather than coping silently."""
    bad_lines = pd.DataFrame({
        "Document_No": ["SO1"], "Line_No": [10000], "Type": ["Item"],
    })
    wb = _write_workbook(tmp_path / "wb.xlsx", lines=bad_lines)
    with pytest.raises(ValueError, match="missing expected columns"):
        read_so_lines(wb)


def test_find_workbook_returns_none_when_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(so_data, "ONEDRIVE_DATA_PATH", tmp_path)  # empty dir
    assert find_workbook() is None


def test_run_logs_warning_when_file_missing(tmp_db, tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(so_data, "ONEDRIVE_DATA_PATH", tmp_path / "nope")  # missing dir
    with caplog.at_level(logging.WARNING):
        run()  # must not raise
    assert any("not found" in r.message for r in caplog.records)
    # Empty tables were written so the rest of the pipeline can run.
    from src.db import get_connection
    conn = get_connection()
    assert conn.execute(f"SELECT COUNT(*) FROM {HEADERS_TABLE}").fetchone()[0] == 0
    assert conn.execute(f"SELECT COUNT(*) FROM {LINES_TABLE}").fetchone()[0] == 0
    conn.close()


def test_run_logs_staleness_warning_when_mtime_old(tmp_db, tmp_path, monkeypatch, caplog):
    folder = tmp_path / "od"
    folder.mkdir()
    wb = _write_workbook(folder / "BC_SalesOrderData.xlsx")
    # Backdate mtime 30 hours.
    old = (dt.datetime.now() - dt.timedelta(hours=30)).timestamp()
    os.utime(wb, (old, old))
    monkeypatch.setattr(so_data, "ONEDRIVE_DATA_PATH", folder)

    with caplog.at_level(logging.WARNING):
        run()  # must not raise
    assert any("stale" in r.message for r in caplog.records)
    # Data still loaded despite staleness.
    from src.db import get_connection
    conn = get_connection()
    assert conn.execute(f"SELECT COUNT(*) FROM {LINES_TABLE}").fetchone()[0] == 5
    conn.close()
