"""Tests for src/calc/variance.py."""
import datetime as dt

import pandas as pd
import pytest

from src.calc.variance import (
    VARIANCE_COLUMNS,
    compute_variance,
)


def _ar_snap(*rows):
    """Rows: (snapshot_date, forecast_week, week_start_date, receipts)."""
    return pd.DataFrame({
        "snapshot_date": [r[0] for r in rows],
        "as_of_date": [r[0] for r in rows],
        "customerNumber": ["CUST-A"] * len(rows),
        "forecast_week": [r[1] for r in rows],
        "week_start_date": [r[2] for r in rows],
        "receipts": [r[3] for r in rows],
    })


def _ap_snap(*rows):
    """Rows: (snapshot_date, forecast_week, week_start_date, disbursements)."""
    return pd.DataFrame({
        "snapshot_date": [r[0] for r in rows],
        "as_of_date": [r[0] for r in rows],
        "vendorNumber": ["VEND-A"] * len(rows),
        "forecast_week": [r[1] for r in rows],
        "week_start_date": [r[2] for r in rows],
        "disbursements": [r[3] for r in rows],
    })


WS1 = "2026-05-25"
WS2 = "2026-06-01"


# ---------------------------------------------------------------------------


def test_basic_two_snapshots_one_day_apart():
    """Deltas = today - prior at the week level."""
    ar = _ar_snap(
        ("2026-05-29", 1, WS1, 1000.0),
        ("2026-05-30", 1, WS1, 1200.0),  # AR up 200
    )
    ap = _ap_snap(
        ("2026-05-29", 1, WS1, 300.0),
        ("2026-05-30", 1, WS1, 350.0),   # AP up 50
    )

    var = compute_variance(ar, ap)

    assert len(var) == 1
    row = var.iloc[0]
    assert row["snapshot_date"] == "2026-05-30"
    assert row["prior_snapshot_date"] == "2026-05-29"
    assert row["ar_receipts_today"] == pytest.approx(1200.0)
    assert row["ar_receipts_prior"] == pytest.approx(1000.0)
    assert row["ar_receipts_delta"] == pytest.approx(200.0)
    assert row["ap_disbursements_delta"] == pytest.approx(50.0)
    # net = AR - AP. today: 1200-350=850 ; prior: 1000-300=700 ; delta +150
    assert row["net_today"] == pytest.approx(850.0)
    assert row["net_prior"] == pytest.approx(700.0)
    assert row["net_delta"] == pytest.approx(150.0)


def test_aggregates_across_entities_to_week_level():
    """Multiple customers/vendors in a week collapse to one variance row."""
    ar = pd.DataFrame({
        "snapshot_date": ["2026-05-29", "2026-05-29", "2026-05-30", "2026-05-30"],
        "customerNumber": ["CUST-A", "CUST-B", "CUST-A", "CUST-B"],
        "forecast_week": [1, 1, 1, 1],
        "week_start_date": [WS1] * 4,
        "receipts": [600.0, 400.0, 700.0, 500.0],  # prior 1000 -> today 1200
    })
    ap = _ap_snap(("2026-05-29", 1, WS1, 0.0), ("2026-05-30", 1, WS1, 0.0))

    var = compute_variance(ar, ap)

    assert len(var) == 1
    assert var.iloc[0]["ar_receipts_today"] == pytest.approx(1200.0)
    assert var.iloc[0]["ar_receipts_delta"] == pytest.approx(200.0)


def test_first_run_only_one_snapshot_is_empty_no_error():
    ar = _ar_snap(("2026-05-30", 1, WS1, 1000.0))
    ap = _ap_snap(("2026-05-30", 1, WS1, 300.0))

    var = compute_variance(ar, ap)

    assert var.empty
    assert list(var.columns) == VARIANCE_COLUMNS  # shape preserved


def test_no_snapshots_at_all_is_empty():
    var = compute_variance(pd.DataFrame(), pd.DataFrame())
    assert var.empty


def test_multiday_gap_uses_most_recent_prior():
    """With snapshots 3+ days apart, the closest prior is used (not the oldest)."""
    ar = _ar_snap(
        ("2026-05-20", 1, WS1, 500.0),   # oldest
        ("2026-05-27", 1, WS1, 800.0),   # most recent prior
        ("2026-05-30", 1, WS1, 1000.0),  # current
    )
    ap = _ap_snap(
        ("2026-05-20", 1, WS1, 100.0),
        ("2026-05-27", 1, WS1, 200.0),
        ("2026-05-30", 1, WS1, 250.0),
    )

    var = compute_variance(ar, ap)

    row = var.iloc[0]
    assert row["snapshot_date"] == "2026-05-30"
    assert row["prior_snapshot_date"] == "2026-05-27"  # not 2026-05-20
    assert row["ar_receipts_prior"] == pytest.approx(800.0)
    assert row["ar_receipts_delta"] == pytest.approx(200.0)


def test_ap_increase_drives_net_delta_negative():
    """Sign correctness: AP up (more cash out) => net_delta negative."""
    ar = _ar_snap(
        ("2026-05-29", 1, WS1, 1000.0),
        ("2026-05-30", 1, WS1, 1000.0),  # AR unchanged
    )
    ap = _ap_snap(
        ("2026-05-29", 1, WS1, 200.0),
        ("2026-05-30", 1, WS1, 500.0),   # AP up 300 -> more cash out
    )

    var = compute_variance(ar, ap)
    row = var.iloc[0]

    assert row["ap_disbursements_delta"] == pytest.approx(300.0)   # AP rose
    assert row["net_delta"] == pytest.approx(-300.0)               # bad for cash
    assert row["net_delta"] < 0


def test_multiple_weeks_each_get_a_row():
    ar = _ar_snap(
        ("2026-05-29", 1, WS1, 100.0), ("2026-05-29", 2, WS2, 200.0),
        ("2026-05-30", 1, WS1, 150.0), ("2026-05-30", 2, WS2, 250.0),
    )
    ap = _ap_snap(
        ("2026-05-29", 1, WS1, 10.0), ("2026-05-29", 2, WS2, 20.0),
        ("2026-05-30", 1, WS1, 10.0), ("2026-05-30", 2, WS2, 20.0),
    )

    var = compute_variance(ar, ap).sort_values("forecast_week").reset_index(drop=True)

    assert list(var["forecast_week"]) == [1, 2]
    assert var.iloc[0]["ar_receipts_delta"] == pytest.approx(50.0)
    assert var.iloc[1]["ar_receipts_delta"] == pytest.approx(50.0)


def test_run_against_tmp_db(tmp_path, monkeypatch):
    """End-to-end run() writes forecast_variance to the (temp) DB."""
    import src.db as db
    monkeypatch.setattr(db, "SQLITE_PATH", tmp_path / "test.db")
    from src.db import get_connection
    from src.calc import variance

    conn = get_connection()
    _ar_snap(
        ("2026-05-29", 1, WS1, 1000.0),
        ("2026-05-30", 1, WS1, 1200.0),
    ).to_sql("ar_receipts_snapshots", conn, if_exists="replace", index=False)
    _ap_snap(
        ("2026-05-29", 1, WS1, 300.0),
        ("2026-05-30", 1, WS1, 350.0),
    ).to_sql("ap_disbursements_snapshots", conn, if_exists="replace", index=False)
    conn.commit()
    conn.close()

    variance.run()
    out = variance.load_table("forecast_variance")

    assert len(out) == 1
    assert out.iloc[0]["net_delta"] == pytest.approx(150.0)
