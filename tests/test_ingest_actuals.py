"""Tests for src/ingest_actuals.py (Actuals tab -> inputs/actuals.json round-trip)."""
import datetime as dt
import json

import pytest
from openpyxl import Workbook

from src.ingest_actuals import merge_into_json, read_actuals_tab, run
from src.output.excel_writer import SHEET_ACTUALS


def _make_actuals_workbook(path, rows):
    """rows: list of (week_start, receipts, disbursements, ending_cash) tuples (None ok)."""
    wb = Workbook()
    ws = wb.active
    ws.title = SHEET_ACTUALS
    ws.append(["Week Start", "Actual Receipts", "Actual Disbursements", "Actual Ending Cash"])
    for wk, rec, disb, end in rows:
        ws.append([wk, rec, disb, end])
    wb.save(path)
    return path


def test_read_full_rows_only(tmp_path):
    wb = _make_actuals_workbook(tmp_path / "wb.xlsx", [
        (dt.date(2026, 5, 25), 1000.0, 600.0, 1500.0),   # complete
        (dt.date(2026, 6, 1), 2000.0, None, None),        # missing disbursements -> skip
        (dt.date(2026, 6, 8), None, None, None),          # blank -> skip
    ])
    entered = read_actuals_tab(wb)
    assert set(entered.keys()) == {"2026-05-25"}
    assert entered["2026-05-25"] == {"receipts": 1000.0, "disbursements": 600.0, "ending_cash": 1500.0}


def test_ending_cash_optional(tmp_path):
    wb = _make_actuals_workbook(tmp_path / "wb.xlsx", [
        (dt.date(2026, 5, 25), 1000.0, 600.0, None),
    ])
    entered = read_actuals_tab(wb)
    assert entered["2026-05-25"] == {"receipts": 1000.0, "disbursements": 600.0}
    assert "ending_cash" not in entered["2026-05-25"]


def test_merge_adds_and_updates_preserving_others(tmp_path):
    jp = tmp_path / "actuals.json"
    jp.write_text(json.dumps({
        "2026-05-18": {"receipts": 1.0, "disbursements": 2.0},   # preserved
        "2026-05-25": {"receipts": 9.0, "disbursements": 9.0},   # will be updated
    }))
    entered = {
        "2026-05-25": {"receipts": 1000.0, "disbursements": 600.0},   # update
        "2026-06-01": {"receipts": 5.0, "disbursements": 6.0},        # add
    }
    added, updated = merge_into_json(entered, jp)
    assert (added, updated) == (1, 1)
    result = json.loads(jp.read_text())
    assert set(result.keys()) == {"2026-05-18", "2026-05-25", "2026-06-01"}
    assert result["2026-05-18"] == {"receipts": 1.0, "disbursements": 2.0}   # untouched
    assert result["2026-05-25"]["receipts"] == 1000.0                         # updated
    # written sorted by week
    assert list(result.keys()) == sorted(result.keys())


def test_merge_no_change_when_identical(tmp_path):
    jp = tmp_path / "actuals.json"
    jp.write_text(json.dumps({"2026-05-25": {"receipts": 1000.0, "disbursements": 600.0}}))
    added, updated = merge_into_json({"2026-05-25": {"receipts": 1000.0, "disbursements": 600.0}}, jp)
    assert (added, updated) == (0, 0)


def test_run_round_trip(tmp_path, monkeypatch):
    wb_path = _make_actuals_workbook(tmp_path / "cashflow_forecast.xlsx", [
        (dt.date(2026, 5, 25), 1000.0, 600.0, 1500.0),
    ])
    jp = tmp_path / "actuals.json"
    import src.ingest_actuals as ing
    monkeypatch.setattr(ing, "ACTUALS_JSON", jp)
    run(workbook_path=wb_path)
    result = json.loads(jp.read_text())
    assert result["2026-05-25"]["receipts"] == 1000.0


def test_run_missing_workbook_no_exception(tmp_path, caplog):
    import logging
    with caplog.at_level(logging.WARNING):
        run(workbook_path=tmp_path / "nope.xlsx")  # must not raise
    assert any("not found" in r.message for r in caplog.records)
