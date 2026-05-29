"""Tests for src/extract/vendor_master.py."""
from pathlib import Path

import pandas as pd

from src.extract.vendor_master import (
    EXPECTED_COLUMNS,
    find_latest_csv,
    read_csv,
)


SAMPLE_CSV = (
    "@odata.etag,ItemInternalId,id,number,displayName,addressLine1,"
    "addressLine2,city,state,country,postalCode,phoneNumber,email,"
    "website,taxRegistrationNumber,currencyId,currencyCode,irs1099Code,"
    "paymentTermsId,paymentMethodId,taxLiable,blocked@odata.type,blocked,"
    "balance,lastModifiedDateTime\n"
    'etag1,internal1,abc-1,1026,Example Vendor A,,,TESTVILLE,IL,US,60067,,,,,'
    "00000000-0000-0000-0000-000000000000,USD,EXEMPT,terms-net30,methodA,"
    "True,#Microsoft.NAV.vendorBlocked,_x0020_,3038.49,2025-02-26T21:16:50.103Z\n"
    'etag2,internal2,abc-2,1047,Example Vendor B,300 Fixture Rd,,EXAMPLE CITY,'
    "CA,US,91748,,,,,00000000-0000-0000-0000-000000000000,USD,EXEMPT,"
    "terms-net30,methodA,True,#Microsoft.NAV.vendorBlocked,All,47008.42,"
    "2026-02-04T19:18:57.853Z\n"
    'etag3,internal3,abc-3,VEND-A,Example Vendor C,,,TEST CITY,IL,US,60601,,,,,'
    "00000000-0000-0000-0000-000000000000,USD,NEC-01,terms-net15,methodB,"
    "True,#Microsoft.NAV.vendorBlocked,Payment,7215282.00,"
    "2026-05-28T10:00:00.000Z\n"
    'etag4,internal4,abc-4,VEND-D,Example Vendor D,,,SAMPLE CITY,GA,US,30309,,,,,'
    "00000000-0000-0000-0000-000000000000,USD,EXEMPT,terms-net30,methodA,"
    "True,#Microsoft.NAV.vendorBlocked,_x0020_,-73516.00,"
    "2026-05-20T08:00:00.000Z\n"
)


def test_read_csv_keeps_only_expected_columns(tmp_path: Path) -> None:
    csv_path = tmp_path / "VendorMaster_2026-05-29.csv"
    csv_path.write_text(SAMPLE_CSV)

    df = read_csv(csv_path)

    assert set(df.columns) == set(EXPECTED_COLUMNS)
    for noise in ("@odata.etag", "ItemInternalId", "blocked@odata.type"):
        assert noise not in df.columns
    for irrelevant in (
        "addressLine1", "city", "state", "phoneNumber", "email", "website",
        "taxRegistrationNumber", "currencyId", "irs1099Code", "paymentMethodId",
        "taxLiable",
    ):
        assert irrelevant not in df.columns


def test_read_csv_parses_types_and_rows(tmp_path: Path) -> None:
    csv_path = tmp_path / "VendorMaster_2026-05-29.csv"
    csv_path.write_text(SAMPLE_CSV)

    df = read_csv(csv_path)

    assert len(df) == 4
    assert df["number"].iloc[0] == "1026"
    assert df["displayName"].iloc[0] == "Example Vendor A"
    assert df["balance"].iloc[0] == 3038.49
    assert df["balance"].iloc[3] == -73516.00
    assert pd.api.types.is_datetime64_any_dtype(df["lastModifiedDateTime"])


def test_read_csv_normalizes_x0020_blocked_to_na(tmp_path: Path) -> None:
    """The PA-encoded `_x0020_` sentinel for unblocked vendors becomes pd.NA.

    Rows with real blocked values ("All", "Payment") flow through unchanged
    so downstream filtering by blocked status still works.
    """
    csv_path = tmp_path / "VendorMaster_2026-05-29.csv"
    csv_path.write_text(SAMPLE_CSV)

    df = read_csv(csv_path)

    assert pd.isna(df.loc[df["number"] == "1026", "blocked"].iloc[0])
    assert pd.isna(df.loc[df["number"] == "VEND-D", "blocked"].iloc[0])
    assert df.loc[df["number"] == "1047", "blocked"].iloc[0] == "All"
    assert df.loc[df["number"] == "VEND-A", "blocked"].iloc[0] == "Payment"
    assert df["blocked"].isna().sum() == 2
    assert df["blocked"].notna().sum() == 2


def test_find_latest_csv_picks_most_recent_date(tmp_path: Path) -> None:
    (tmp_path / "VendorMaster_2026-05-27.csv").write_text(SAMPLE_CSV)
    (tmp_path / "VendorMaster_2026-05-29.csv").write_text(SAMPLE_CSV)
    (tmp_path / "VendorMaster_2026-05-28.csv").write_text(SAMPLE_CSV)

    latest = find_latest_csv(tmp_path)

    assert latest is not None
    assert latest.name == "VendorMaster_2026-05-29.csv"


def test_find_latest_csv_ignores_other_flow_outputs(tmp_path: Path) -> None:
    (tmp_path / "VendorMaster_2026-05-29.csv").write_text(SAMPLE_CSV)
    (tmp_path / "CustomerMaster_2026-05-29.csv").write_text("a,b\n1,2\n")
    (tmp_path / "AP_vendorLedgerEntries_2026-05-29.csv").write_text("a,b\n1,2\n")
    (tmp_path / "AP_History_2026-05-29.csv").write_text("a,b\n1,2\n")
    (tmp_path / "PaymentTerms_2026-05-29.csv").write_text("a,b\n1,2\n")

    latest = find_latest_csv(tmp_path)

    assert latest is not None
    assert latest.name == "VendorMaster_2026-05-29.csv"


def test_find_latest_csv_returns_none_when_no_match(tmp_path: Path) -> None:
    (tmp_path / "CustomerMaster_2026-05-29.csv").write_text("a,b\n1,2\n")
    (tmp_path / "other.csv").write_text("a,b\n1,2\n")

    assert find_latest_csv(tmp_path) is None