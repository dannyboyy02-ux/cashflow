"""Tests for src/extract/ar_ledger.py."""
from pathlib import Path

import pandas as pd
import pytest

from src.extract.ar_ledger import (
    EXPECTED_COLUMNS,
    FILENAME_PATTERN,
    find_latest_csv,
    read_csv,
)


SAMPLE_CSV = (
    "ItemInternalId,entryNumber,documentType,description,postingDate,documentNumber,"
    "externalDocumentNumber,balancingAccountNumber,balancingAccountType,customerNumber,"
    "open,dimensionSetID,currencyCode,yourReference,lastModifiedDateTime,amount,"
    "debitAmount,creditAmount,amountLocalCurrency,debitAmountLocalCurrency,"
    "creditAmountLocalCurrency\n"
    "32571394,32571394,Invoice,Invoice 215830,2023-09-24,215830,511480,11100,"
    "G/L Account,CUST-A,true,37,,,2024-06-19T17:43:43.153Z,1806.0,1806.0,0.0,"
    "1806.0,1806.0,0.0\n"
)


def test_read_csv_returns_dataframe_with_expected_shape(tmp_path: Path) -> None:
    csv_path = tmp_path / "AR_customerLedgerEntries_2026-05-27.csv"
    csv_path.write_text(SAMPLE_CSV)

    df = read_csv(csv_path)

    assert len(df) == 1
    assert set(df.columns) == set(EXPECTED_COLUMNS)
    assert df.loc[0, "customerNumber"] == "CUST-A"
    assert df.loc[0, "amount"] == 1806.0
    assert df.loc[0, "open"] == True
    assert str(df.loc[0, "postingDate"]) == "2023-09-24"


def test_find_latest_csv_picks_most_recent_date(tmp_path: Path) -> None:
    (tmp_path / "AR_customerLedgerEntries_2026-05-25.csv").write_text(SAMPLE_CSV)
    (tmp_path / "AR_customerLedgerEntries_2026-05-27.csv").write_text(SAMPLE_CSV)
    (tmp_path / "AR_customerLedgerEntries_2026-05-26.csv").write_text(SAMPLE_CSV)

    latest = find_latest_csv(tmp_path)

    assert latest is not None
    assert latest.name == "AR_customerLedgerEntries_2026-05-27.csv"


def test_find_latest_csv_ignores_non_matching_files(tmp_path: Path) -> None:
    (tmp_path / "AR_customerLedgerEntries_2026-05-27.csv").write_text(SAMPLE_CSV)
    (tmp_path / "other.csv").write_text("a,b\n1,2\n")
    (tmp_path / "AR_vendorLedgerEntries_2026-05-27.csv").write_text(SAMPLE_CSV)

    latest = find_latest_csv(tmp_path)

    assert latest is not None
    assert latest.name == "AR_customerLedgerEntries_2026-05-27.csv"


def test_find_latest_csv_returns_none_when_no_match(tmp_path: Path) -> None:
    (tmp_path / "other.csv").write_text("a,b\n1,2\n")
    (tmp_path / "AR_vendorLedgerEntries_2026-05-27.csv").write_text(SAMPLE_CSV)

    latest = find_latest_csv(tmp_path)

    assert latest is None