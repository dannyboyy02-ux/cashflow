"""Tests for src/extract/ar_history.py."""
from pathlib import Path

import pandas as pd
import pytest

from src.extract.ar_history import (
    EXPECTED_COLUMNS,
    find_latest_csv,
    read_csv,
)

# Fixture mirrors the real Flow BC_AR_History output: same 21-column shape as
# Flow 1, but rows span all document types (Invoice, Credit Memo, Payment,
# Refund, and a rare blank-documentType journal entry) and both open/closed
# states -- the conditions ar_ledger.py never sees because Flow 1 filters to
# open invoices only.
SAMPLE_CSV = (
    "ItemInternalId,entryNumber,documentType,description,postingDate,"
    "documentNumber,externalDocumentNumber,balancingAccountNumber,"
    "balancingAccountType,customerNumber,open,dimensionSetID,currencyCode,"
    "yourReference,lastModifiedDateTime,amount,debitAmount,creditAmount,"
    "amountLocalCurrency,debitAmountLocalCurrency,creditAmountLocalCurrency\n"
    "40000001,40000001,Invoice,Invoice 300001,2026-04-21,300001,EXT001,11100,"
    "G/L Account,CUST-A,false,37,,,2026-05-15T10:00:00.000Z,11343.93,11343.93,"
    "0.0,11343.93,11343.93,0.0\n"
    "40000002,40000002,Credit Memo,CM 300002,2026-04-25,300002,EXT002,11100,"
    "G/L Account,CUST-A,false,37,,,2026-05-15T10:00:00.000Z,-527.25,0.0,527.25,"
    "-527.25,0.0,527.25\n"
    "40000003,40000003,Payment,Payment from CUST-A,2026-05-12,PMT100012,,11100,"
    "G/L Account,CUST-A,false,37,,,2026-05-12T11:00:00.000Z,-10816.68,0.0,"
    "10816.68,-10816.68,0.0,10816.68\n"
    "40000004,40000004,Refund,Refund to CUST-C,2026-05-20,REF400023,,11100,"
    "G/L Account,CUST-C,false,37,,,2026-05-20T14:00:00.000Z,150.00,150.00,0.0,"
    "150.00,150.00,0.0\n"
    "40000005,40000005,,Opening balance journal,2026-01-15,JNL001,,11100,"
    "G/L Account,CUST-Z,true,37,,,2026-01-15T09:00:00.000Z,250.00,250.00,0.0,"
    "250.00,250.00,0.0\n"
)


def test_read_csv_returns_all_document_types(tmp_path: Path) -> None:
    csv_path = tmp_path / "AR_History_2026-05-29.csv"
    csv_path.write_text(SAMPLE_CSV)

    df = read_csv(csv_path)

    assert len(df) == 5
    assert set(df.columns) == set(EXPECTED_COLUMNS)
    doc_types = set(df["documentType"].fillna("(blank)").tolist())
    assert doc_types == {"Invoice", "Credit Memo", "Payment", "Refund", "(blank)"}


def test_read_csv_preserves_signed_amounts(tmp_path: Path) -> None:
    """Net credit sales = Invoice + Credit Memo with signs intact.

    For customer CUST-A in this fixture: 11343.93 invoice + -527.25 credit memo
    = 10816.68 net sales. The DSO calc relies on this signed sum behavior.
    """
    csv_path = tmp_path / "AR_History_2026-05-29.csv"
    csv_path.write_text(SAMPLE_CSV)

    df = read_csv(csv_path)

    sales = df[df["documentType"].isin(["Invoice", "Credit Memo"])]
    customer_4002 = sales[sales["customerNumber"] == "CUST-A"]["amount"].sum()
    assert customer_4002 == pytest.approx(10816.68)


def test_read_csv_parses_mixed_open_and_closed(tmp_path: Path) -> None:
    """Unlike Flow 1's open-only data, history has both states."""
    csv_path = tmp_path / "AR_History_2026-05-29.csv"
    csv_path.write_text(SAMPLE_CSV)

    df = read_csv(csv_path)

    assert df["open"].sum() == 1
    assert (~df["open"]).sum() == 4


def test_find_latest_csv_picks_most_recent_date(tmp_path: Path) -> None:
    (tmp_path / "AR_History_2026-05-27.csv").write_text(SAMPLE_CSV)
    (tmp_path / "AR_History_2026-05-29.csv").write_text(SAMPLE_CSV)
    (tmp_path / "AR_History_2026-05-28.csv").write_text(SAMPLE_CSV)

    latest = find_latest_csv(tmp_path)

    assert latest is not None
    assert latest.name == "AR_History_2026-05-29.csv"


def test_find_latest_csv_ignores_non_matching_files(tmp_path: Path) -> None:
    (tmp_path / "AR_History_2026-05-29.csv").write_text(SAMPLE_CSV)
    (tmp_path / "AR_customerLedgerEntries_2026-05-29.csv").write_text(SAMPLE_CSV)
    (tmp_path / "CustomerMaster_2026-05-29.csv").write_text("a,b\n1,2\n")

    latest = find_latest_csv(tmp_path)

    assert latest is not None
    assert latest.name == "AR_History_2026-05-29.csv"


def test_find_latest_csv_returns_none_when_no_match(tmp_path: Path) -> None:
    (tmp_path / "AR_customerLedgerEntries_2026-05-29.csv").write_text(SAMPLE_CSV)

    assert find_latest_csv(tmp_path) is None