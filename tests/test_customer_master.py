"""Tests for src/extract/customer_master.py."""
from pathlib import Path

import pandas as pd
import pytest

from src.extract.customer_master import (
    EXPECTED_COLUMNS,
    find_latest_csv,
    read_csv,
)

# Fixture mirrors the real v2.0 customers CSV: full header including the
# serialization noise (@odata.etag, ItemInternalId, type@odata.type,
# blocked@odata.type) that read_csv must drop. Two rows: a NET30 customer and a
# blank-terms customer (zero-GUID), so the blank-terms path is covered.
SAMPLE_CSV = (
    "@odata.etag,ItemInternalId,id,number,displayName,type@odata.type,type,"
    "addressLine1,addressLine2,city,state,country,postalCode,phoneNumber,email,"
    "website,salespersonCode,balanceDue,creditLimit,taxLiable,taxAreaId,"
    "taxAreaDisplayName,taxRegistrationNumber,currencyId,currencyCode,"
    "paymentTermsId,shipmentMethodId,paymentMethodId,blocked@odata.type,blocked,"
    "lastModifiedDateTime\n"
    'W/"JzIw",aaaa1111,11110000-1111-2222-3333-444455556666,CUST-A,Example Customer A,'
    "#Microsoft.NAV.customerType,Company,100 Example St,,SAMPLE CITY,OH,US,43004,,,,SP1,"
    "15000.50,50000,true,,,,,USD,terms-guid-net30-0000-0000-000000000001,,,"
    "#Microsoft.NAV.blocked, ,2026-01-16T17:34:06.047Z\n"
    'W/"JzIx",bbbb2222,22220000-1111-2222-3333-444455556666,CUST-B,Example Customer B,'
    "#Microsoft.NAV.customerType,Company,200 Sample Ave,,TESTVILLE,CA,US,93901,,,,SP2,0,"
    "25000,true,,,,,USD,00000000-0000-0000-0000-000000000000,,,"
    "#Microsoft.NAV.blocked, ,2026-02-08T22:48:56.650Z\n"
)


def test_read_csv_keeps_only_expected_columns(tmp_path: Path) -> None:
    csv_path = tmp_path / "CustomerMaster_2026-05-28.csv"
    csv_path.write_text(SAMPLE_CSV)

    df = read_csv(csv_path)

    assert set(df.columns) == set(EXPECTED_COLUMNS)
    for noise in ("@odata.etag", "ItemInternalId", "type@odata.type", "blocked@odata.type"):
        assert noise not in df.columns


def test_read_csv_parses_types_and_rows(tmp_path: Path) -> None:
    csv_path = tmp_path / "CustomerMaster_2026-05-28.csv"
    csv_path.write_text(SAMPLE_CSV)

    df = read_csv(csv_path)

    assert len(df) == 2
    assert df.loc[0, "number"] == "CUST-A"
    assert df.loc[0, "paymentTermsId"] == "terms-guid-net30-0000-0000-000000000001"
    assert df.loc[0, "balanceDue"] == 15000.50
    assert df.loc[1, "paymentTermsId"] == "00000000-0000-0000-0000-000000000000"


def test_find_latest_csv_picks_most_recent_date(tmp_path: Path) -> None:
    (tmp_path / "CustomerMaster_2026-05-26.csv").write_text(SAMPLE_CSV)
    (tmp_path / "CustomerMaster_2026-05-28.csv").write_text(SAMPLE_CSV)
    (tmp_path / "CustomerMaster_2026-05-27.csv").write_text(SAMPLE_CSV)

    latest = find_latest_csv(tmp_path)

    assert latest is not None
    assert latest.name == "CustomerMaster_2026-05-28.csv"


def test_find_latest_csv_ignores_non_matching_files(tmp_path: Path) -> None:
    (tmp_path / "CustomerMaster_2026-05-28.csv").write_text(SAMPLE_CSV)
    (tmp_path / "PaymentTerms_2026-05-28.csv").write_text("a,b\n1,2\n")
    (tmp_path / "other.csv").write_text("a,b\n1,2\n")

    latest = find_latest_csv(tmp_path)

    assert latest is not None
    assert latest.name == "CustomerMaster_2026-05-28.csv"


def test_find_latest_csv_returns_none_when_no_match(tmp_path: Path) -> None:
    (tmp_path / "PaymentTerms_2026-05-28.csv").write_text("a,b\n1,2\n")

    assert find_latest_csv(tmp_path) is None