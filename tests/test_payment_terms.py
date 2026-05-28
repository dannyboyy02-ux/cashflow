"""Tests for src/extract/payment_terms.py."""
from pathlib import Path

import pandas as pd
import pytest

from src.extract.payment_terms import (
    EXPECTED_COLUMNS,
    find_latest_csv,
    read_csv,
)

# Full 10-column header (including @odata.etag and ItemInternalId noise) with the
# in-use terms plus extras. Values drawn from the real PaymentTerms extract.
SAMPLE_CSV = (
    "@odata.etag,ItemInternalId,id,code,displayName,dueDateCalculation,"
    "discountDateCalculation,discountPercent,calculateDiscountOnCreditMemos,"
    "lastModifiedDateTime\n"
    'W/"Jz04",pt000004,b10fe290-5f6a-eb11-aa81-000d3afcdb74,NET30,Net 30 Days,30D,,0,'
    "FALSE,2021-02-08T22:48:56.650Z\n"
    'W/"Jz05",pt000005,caf8d2d2-5f6a-eb11-aa81-000d3afcdb74,ON RECPT,Due upon receipt,1D,,0,'
    "FALSE,2021-02-08T22:49:06.927Z\n"
    'W/"Jz02",pt000002,c17fdc51-5f6a-eb11-aa81-000d3afcdb74,NET10,Net 10 Days,10D,,0,'
    "FALSE,2021-02-08T22:45:35.027Z\n"
)


def test_read_csv_keeps_only_expected_columns(tmp_path: Path) -> None:
    csv_path = tmp_path / "PaymentTerms_2026-05-28.csv"
    csv_path.write_text(SAMPLE_CSV)

    df = read_csv(csv_path)

    assert set(df.columns) == set(EXPECTED_COLUMNS)
    assert "@odata.etag" not in df.columns
    assert "ItemInternalId" not in df.columns


def test_read_csv_preserves_due_date_formula(tmp_path: Path) -> None:
    csv_path = tmp_path / "PaymentTerms_2026-05-28.csv"
    csv_path.write_text(SAMPLE_CSV)

    df = read_csv(csv_path)

    assert len(df) == 3
    terms = dict(zip(df["code"], df["dueDateCalculation"]))
    assert terms["NET30"] == "30D"
    assert terms["NET10"] == "10D"
    assert terms["ON RECPT"] == "1D"


def test_find_latest_csv_picks_most_recent_date(tmp_path: Path) -> None:
    (tmp_path / "PaymentTerms_2026-05-26.csv").write_text(SAMPLE_CSV)
    (tmp_path / "PaymentTerms_2026-05-28.csv").write_text(SAMPLE_CSV)

    latest = find_latest_csv(tmp_path)

    assert latest is not None
    assert latest.name == "PaymentTerms_2026-05-28.csv"


def test_find_latest_csv_returns_none_when_no_match(tmp_path: Path) -> None:
    (tmp_path / "CustomerMaster_2026-05-28.csv").write_text("a,b\n1,2\n")

    assert find_latest_csv(tmp_path) is None