"""Tests for src/output/excel_writer.py.

These use synthetic in-horizon fixtures (mirroring the repo's pytest tmp_path
pattern) rather than the live SQLite tables, so the assertions are isolated and
deterministic. "Totals match the aggregate" and "row count == distinct entities
with in-horizon cash" are checked against the same fixture frames the workbook
is built from.
"""
import datetime as dt

import pandas as pd
import pytest
from openpyxl import load_workbook

from src.output.excel_writer import (
    OUTPUT_FILENAME,
    SHEET_AP,
    SHEET_AR,
    SHEET_FORECAST,
    SHEET_NOTES,
    build_workbook,
    latest_refresh_timestamp,
    run,
    write_workbook,
)


AS_OF = dt.date(2026, 5, 29)  # Friday; week-1 Monday = 2026-05-25


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _receipts():
    """ar_receipts_by_week shape: 3 customers across a few weeks."""
    return pd.DataFrame({
        "customerNumber": ["CUST-A", "CUST-A", "CUST-B", "CUST-C"],
        "forecast_week":  [1, 3, 2, 13],
        "week_start_date": [
            dt.date(2026, 5, 25), dt.date(2026, 6, 8),
            dt.date(2026, 6, 1), dt.date(2026, 8, 17),
        ],
        "receipts": [10_000.0, 5_000.0, 8_000.0, 2_000.0],
    })


def _disbursements():
    """ap_disbursements_by_week shape: 2 vendors."""
    return pd.DataFrame({
        "vendorNumber": ["VEND-A", "VEND-A", "VEND-B"],
        "forecast_week": [1, 2, 2],
        "week_start_date": [
            dt.date(2026, 5, 25), dt.date(2026, 6, 1), dt.date(2026, 6, 1),
        ],
        "disbursements": [3_000.0, 9_000.0, 1_500.0],
    })


def _customers():
    return pd.DataFrame({
        "number": ["CUST-A", "CUST-B", "CUST-C"],
        "displayName": ["Alpha Foods", "Beta Grocers", "Gamma Mart"],
    })


def _vendors():
    return pd.DataFrame({
        "number": ["VEND-A", "VEND-B"],
        "displayName": ["Acme Supply", "Borden Logistics"],
    })


def _build(tmp_path):
    """Build the workbook from fixtures, save it, and return the loaded copy."""
    wb = build_workbook(
        _receipts(), _disbursements(), _customers(), _vendors(),
        AS_OF, refresh_ts=dt.datetime(2026, 5, 29, 12, 0, 0),
    )
    path = tmp_path / "forecast.xlsx"
    write_workbook(wb, path)
    return load_workbook(path)


# ---------------------------------------------------------------------------
# Structure
# ---------------------------------------------------------------------------


def test_workbook_created_at_expected_path(tmp_path):
    path = tmp_path / "sub" / "forecast.xlsx"
    wb = build_workbook(
        _receipts(), _disbursements(), _customers(), _vendors(), AS_OF,
    )
    saved = write_workbook(wb, path)

    assert saved.exists()
    assert saved.name == "forecast.xlsx"


def test_run_writes_to_default_data_dir_filename(tmp_path):
    """run() with an explicit path produces a file with the module's filename."""
    out = tmp_path / OUTPUT_FILENAME
    saved = run(as_of_date=AS_OF, output_path=out)

    assert saved.exists()
    assert saved.name == OUTPUT_FILENAME


def test_all_four_sheets_exist_with_expected_names(tmp_path):
    wb = _build(tmp_path)
    assert wb.sheetnames == [SHEET_FORECAST, SHEET_AR, SHEET_AP, SHEET_NOTES]


def test_forecast_sheet_has_header_13_weeks_and_totals_row(tmp_path):
    wb = _build(tmp_path)
    ws = wb[SHEET_FORECAST]

    # 1 header + 13 week rows + 1 totals row = 15
    assert ws.max_row == 15
    # Week numbers 1..13 in column A of the data rows
    assert [ws.cell(row=r, column=1).value for r in range(2, 15)] == list(range(1, 14))
    assert ws.cell(row=15, column=1).value == "TOTAL"


def test_forecast_week_start_dates_are_mondays_seven_days_apart(tmp_path):
    wb = _build(tmp_path)
    ws = wb[SHEET_FORECAST]

    wk1 = ws.cell(row=2, column=2).value
    wk2 = ws.cell(row=3, column=2).value
    assert dt.date(wk1.year, wk1.month, wk1.day) == dt.date(2026, 5, 25)  # Monday
    assert (wk2 - wk1).days == 7


# ---------------------------------------------------------------------------
# Numerical correctness
# ---------------------------------------------------------------------------


def test_forecast_totals_match_aggregates(tmp_path):
    wb = _build(tmp_path)
    ws = wb[SHEET_FORECAST]

    expected_rec = _receipts()["receipts"].sum()       # 25,000
    expected_dis = _disbursements()["disbursements"].sum()  # 13,500

    assert ws.cell(row=15, column=4).value == pytest.approx(expected_rec)
    assert ws.cell(row=15, column=5).value == pytest.approx(expected_dis)
    assert ws.cell(row=15, column=6).value == pytest.approx(expected_rec - expected_dis)


def test_forecast_per_week_receipts_and_net(tmp_path):
    wb = _build(tmp_path)
    ws = wb[SHEET_FORECAST]

    # Week 1 (row 2): receipts 10,000 ; disbursements 3,000 ; net 7,000
    assert ws.cell(row=2, column=4).value == pytest.approx(10_000.0)
    assert ws.cell(row=2, column=5).value == pytest.approx(3_000.0)
    assert ws.cell(row=2, column=6).value == pytest.approx(7_000.0)
    # Week 2 (row 3): receipts 8,000 ; disbursements 10,500 ; net -2,500
    assert ws.cell(row=3, column=4).value == pytest.approx(8_000.0)
    assert ws.cell(row=3, column=5).value == pytest.approx(10_500.0)
    assert ws.cell(row=3, column=6).value == pytest.approx(-2_500.0)


def test_beginning_ending_cash_use_formulas(tmp_path):
    """The CFO-input cash chain is live formulas, not baked values."""
    wb = _build(tmp_path)
    ws = wb[SHEET_FORECAST]

    # Week 1 beginning is the 0 placeholder; ending is a formula.
    assert ws.cell(row=2, column=3).value == 0
    assert ws.cell(row=2, column=7).value == "=C2+F2"
    # Week 2 beginning chains off week 1 ending.
    assert ws.cell(row=3, column=3).value == "=G2"
    assert ws.cell(row=3, column=7).value == "=C3+F3"


# ---------------------------------------------------------------------------
# Entity sheets
# ---------------------------------------------------------------------------


def test_ar_sheet_row_count_equals_distinct_customers(tmp_path):
    wb = _build(tmp_path)
    ws = wb[SHEET_AR]

    distinct = _receipts()["customerNumber"].nunique()  # 3
    assert ws.max_row == distinct + 1  # + header


def test_ap_sheet_row_count_equals_distinct_vendors(tmp_path):
    wb = _build(tmp_path)
    ws = wb[SHEET_AP]

    distinct = _disbursements()["vendorNumber"].nunique()  # 2
    assert ws.max_row == distinct + 1


def test_ar_sheet_sorted_by_total_descending_with_names(tmp_path):
    wb = _build(tmp_path)
    ws = wb[SHEET_AR]

    # CUST-A total = 15,000 (largest) -> first data row, joined to its name.
    assert ws.cell(row=2, column=1).value == "CUST-A"
    assert ws.cell(row=2, column=2).value == "Alpha Foods"
    last_col = ws.max_column
    totals = [ws.cell(row=r, column=last_col).value for r in range(2, ws.max_row + 1)]
    assert totals == sorted(totals, reverse=True)
    assert totals[0] == pytest.approx(15_000.0)


def test_ap_sheet_has_week_columns_and_total(tmp_path):
    wb = _build(tmp_path)
    ws = wb[SHEET_AP]

    # Header: Vendor Number, Vendor Name, Week 1..13, Total = 16 columns
    assert ws.max_column == 16
    assert ws.cell(row=1, column=1).value == "Vendor Number"
    assert ws.cell(row=1, column=16).value == "Total"
    # VEND-A total = 12,000 (3,000 wk1 + 9,000 wk2)
    assert ws.cell(row=2, column=1).value == "VEND-A"
    assert ws.cell(row=2, column=16).value == pytest.approx(12_000.0)


# ---------------------------------------------------------------------------
# Notes sheet
# ---------------------------------------------------------------------------


def test_notes_sheet_contains_as_of_date(tmp_path):
    wb = _build(tmp_path)
    ws = wb[SHEET_NOTES]

    text = "\n".join(
        str(ws.cell(row=r, column=1).value or "")
        for r in range(1, ws.max_row + 1)
    )
    assert AS_OF.isoformat() in text


def test_notes_sheet_mentions_methodology_and_caveats(tmp_path):
    wb = _build(tmp_path)
    ws = wb[SHEET_NOTES]

    text = "\n".join(
        str(ws.cell(row=r, column=1).value or "")
        for r in range(1, ws.max_row + 1)
    )
    assert "Methodology" in text
    assert "Caveats" in text
    assert "payroll" in text


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_empty_inputs_still_produce_four_sheets(tmp_path):
    """No receipts/disbursements -> valid workbook, forecast all zeros."""
    empty_rec = pd.DataFrame(columns=["customerNumber", "forecast_week", "week_start_date", "receipts"])
    empty_dis = pd.DataFrame(columns=["vendorNumber", "forecast_week", "week_start_date", "disbursements"])

    wb = build_workbook(empty_rec, empty_dis, _customers(), _vendors(), AS_OF)
    path = tmp_path / "empty.xlsx"
    write_workbook(wb, path)
    loaded = load_workbook(path)

    assert loaded.sheetnames == [SHEET_FORECAST, SHEET_AR, SHEET_AP, SHEET_NOTES]
    ws = loaded[SHEET_FORECAST]
    assert ws.max_row == 15  # header + 13 + totals
    assert ws.cell(row=15, column=4).value == pytest.approx(0.0)
    # Entity sheets have only their header row.
    assert loaded[SHEET_AR].max_row == 1
    assert loaded[SHEET_AP].max_row == 1


def test_latest_refresh_timestamp_missing_folder_returns_none(tmp_path):
    missing = tmp_path / "does-not-exist"
    assert latest_refresh_timestamp(missing) is None


def test_latest_refresh_timestamp_picks_newest_csv(tmp_path):
    import os, time

    older = tmp_path / "AR_History_2026-05-01.csv"
    newer = tmp_path / "AP_History_2026-05-29.csv"
    older.write_text("x")
    newer.write_text("y")
    # Force a clear mtime ordering.
    os.utime(older, (time.time() - 1000, time.time() - 1000))

    ts = latest_refresh_timestamp(tmp_path)
    assert ts is not None
    assert isinstance(ts, dt.datetime)
