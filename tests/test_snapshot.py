"""Tests for src/calc/snapshot.py.

Isolated to a temporary SQLite database (monkeypatching src.db.SQLITE_PATH) so
the snapshot history in the real project DB is never touched by the test suite.
"""
import datetime as dt

import pandas as pd
import pytest

from src.calc import snapshot
from src.calc.snapshot import (
    AP_SNAPSHOT_TABLE,
    AR_SNAPSHOT_TABLE,
    run,
)


D1 = dt.date(2026, 5, 29)
D2 = dt.date(2026, 5, 30)
AS_OF = dt.date(2026, 5, 29)


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """Point get_connection() at a throwaway DB for the duration of a test."""
    import src.db as db
    monkeypatch.setattr(db, "SQLITE_PATH", tmp_path / "test.db")
    return tmp_path / "test.db"


def _ar_source():
    return pd.DataFrame({
        "customerNumber": ["CUST-A", "CUST-B", "CUST-A"],
        "forecast_week": [1, 2, 3],
        "week_start_date": ["2026-05-25", "2026-06-01", "2026-06-08"],
        "receipts": [100.0, 200.0, 50.0],
    })


def _ap_source():
    return pd.DataFrame({
        "vendorNumber": ["VEND-A", "VEND-B"],
        "forecast_week": [1, 2],
        "week_start_date": ["2026-05-25", "2026-06-01"],
        "disbursements": [40.0, 60.0],
    })


def _write_sources(ar=None, ap=None):
    """Seed the bucketed source tables in the (already patched) tmp DB."""
    from src.db import get_connection
    conn = get_connection()
    (ar if ar is not None else _ar_source()).to_sql(
        "ar_receipts_by_week", conn, if_exists="replace", index=False)
    (ap if ap is not None else _ap_source()).to_sql(
        "ap_disbursements_by_week", conn, if_exists="replace", index=False)
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------


def test_basic_snapshot_row_counts_match_sources(tmp_db):
    _write_sources()
    run(snapshot_date=D1, as_of_date=AS_OF)

    ar = snapshot.load_table(AR_SNAPSHOT_TABLE)
    ap = snapshot.load_table(AP_SNAPSHOT_TABLE)

    assert len(ar) == len(_ar_source())  # 3
    assert len(ap) == len(_ap_source())  # 2
    # The date stamps are present and correct on every row.
    assert set(ar["snapshot_date"]) == {D1.isoformat()}
    assert set(ar["as_of_date"]) == {AS_OF.isoformat()}


def test_idempotent_rerun_same_date_does_not_duplicate(tmp_db):
    _write_sources()
    run(snapshot_date=D1, as_of_date=AS_OF)
    run(snapshot_date=D1, as_of_date=AS_OF)  # same day, again

    ar = snapshot.load_table(AR_SNAPSHOT_TABLE)
    ap = snapshot.load_table(AP_SNAPSHOT_TABLE)

    assert len(ar) == len(_ar_source())  # still 3, not 6
    assert len(ap) == len(_ap_source())  # still 2, not 4
    assert ar["snapshot_date"].nunique() == 1


def test_multiday_history_preserves_prior_snapshots(tmp_db):
    _write_sources()
    run(snapshot_date=D1, as_of_date=AS_OF)
    # A later run on a different snapshot_date (forecast itself unchanged here).
    run(snapshot_date=D2, as_of_date=D2)

    ar = snapshot.load_table(AR_SNAPSHOT_TABLE)

    assert len(ar) == 2 * len(_ar_source())  # both batches retained: 6
    assert set(ar["snapshot_date"]) == {D1.isoformat(), D2.isoformat()}
    assert ar["snapshot_date"].nunique() == 2


def test_snapshot_schema_columns_and_dtypes(tmp_db):
    _write_sources()
    run(snapshot_date=D1, as_of_date=AS_OF)

    ar = snapshot.load_table(AR_SNAPSHOT_TABLE)
    ap = snapshot.load_table(AP_SNAPSHOT_TABLE)

    assert list(ar.columns) == [
        "snapshot_date", "as_of_date", "customerNumber",
        "forecast_week", "week_start_date", "receipts",
    ]
    assert list(ap.columns) == [
        "snapshot_date", "as_of_date", "vendorNumber",
        "forecast_week", "week_start_date", "disbursements",
    ]
    assert pd.api.types.is_integer_dtype(ar["forecast_week"])
    assert pd.api.types.is_float_dtype(ar["receipts"])
    assert pd.api.types.is_float_dtype(ap["disbursements"])


def test_empty_source_produces_empty_snapshot_without_error(tmp_db):
    """An empty forecast still creates the snapshot tables (0 rows), no crash."""
    empty_ar = pd.DataFrame(columns=["customerNumber", "forecast_week", "week_start_date", "receipts"])
    empty_ap = pd.DataFrame(columns=["vendorNumber", "forecast_week", "week_start_date", "disbursements"])
    _write_sources(empty_ar, empty_ap)

    run(snapshot_date=D1, as_of_date=AS_OF)

    assert len(snapshot.load_table(AR_SNAPSHOT_TABLE)) == 0
    assert len(snapshot.load_table(AP_SNAPSHOT_TABLE)) == 0
