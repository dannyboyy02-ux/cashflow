"""Tests for src/extract/ap_history.py."""
import datetime as dt
from pathlib import Path

import pandas as pd

from src.extract.ap_history import (
    EXPECTED_COLUMNS,
    find_latest_csv,
    read_csv,
)


SAMPLE_CSV = (
    "ItemInternalId,entryNumber,documentType,description,postingDate,"
    "documentNumber,externalDocumentNumber,balancingAccountNumber,"
    "balancingAccountType,vendorNumber,open,dimensionSetID,currencyCode,"
    "lastModifiedDateTime,amount,debitAmount,creditAmount,"
    "amountLocalCurrency,debitAmountLocalCurrency,creditAmountLocalCurrency\n"
    "100,100,Invoice,Inv A1,2025-09-15,A1,A1,21100,G/L Account,VEND-A,false,"
    "0,,2025-09-15T10:00:00Z,-50000,0,50000,-50000,0,50000\n"
    "101,101,Invoice,Inv A2,2026-04-01,A2,A2,21100,G/L Account,VEND-A,true,"
    "0,,2026-04-01T10:00:00Z,-25000,0,25000,-25000,0,25000\n"
    "102,102,Payment,Pmt P1,2026-01-15,P1,,21100,G/L Account,VEND-A,false,"
    "0,,2026-01-15T10:00:00Z,50000,50000,0,50000,50000,0\n"
    "103,103,Credit Memo,CM C1,2026-03-10,C1,,21100,G/L Account,VEND-A,false,"
    "0,,2026-03-10T10:00:00Z,2000,2000,0,2000,2000,0\n"
    "104,104,Payment,Pmt P2,2026-06-01,P2,,21100,G/L Account,VEND-B,false,"
    "0,,2026-05-28T10:00:00Z,75000,75000,0,75000,75000,0\n"
    "105,105,Invoice,Book Inv,2028-10-28,BI,REF-2024,,G/L Account,VEND-C,false,"
    "0,,2024-10-28T10:00:00Z,-2400,0,2400,-2400,0,2400\n"
    "106,106,Credit Memo,Book CM,2028-10-28,BCM,REF-2024,,G/L Account,VEND-C,"
    "false,0,,2024-10-28T10:00:00Z,2400,2400,0,2400,2400,0\n"
    "107,107,,Manual JE,2026-02-15,MJ1,,21100,G/L Account,VEND-E,false,"
    "0,,2026-02-15T10:00:00Z,-500,0,500,-500,0,500\n"
    "108,108,Refund,Refund R1,2026-04-20,R1,,21100,G/L Account,VEND-E,false,"
    "0,,2026-04-20T10:00:00Z,100,100,0,100,100,0\n"
)


def test_read_csv_returns_all_document_types(tmp_path: Path) -> None:
    """Unlike Flow 2 (open-only), AP_History includes all document types."""
    csv_path = tmp_path / "AP_History_2026-05-29.csv"
    csv_path.write_text(SAMPLE_CSV)

    df = read_csv(csv_path)

    assert len(df) == 9
    doctypes = set(df["documentType"].dropna().unique())
    assert doctypes == {"Invoice", "Payment", "Credit Memo", "Refund"}
    assert df["documentType"].isna().sum() == 1


def test_read_csv_preserves_ap_sign_convention(tmp_path: Path) -> None:
    """Invoices negative, payments/credit memos positive -- AP convention.

    Critical for the DPO calc which uses `-sum(amount)` on Invoice + Credit
    Memo rows to get trailing-net-purchases as a positive number.
    """
    csv_path = tmp_path / "AP_History_2026-05-29.csv"
    csv_path.write_text(SAMPLE_CSV)

    df = read_csv(csv_path)

    invoices = df[df["documentType"] == "Invoice"]
    assert (invoices["amount"] < 0).all()
    payments = df[df["documentType"] == "Payment"]
    assert (payments["amount"] > 0).all()
    credit_memos = df[df["documentType"] == "Credit Memo"]
    assert (credit_memos["amount"] > 0).all()


def test_read_csv_parses_mixed_open_and_closed(tmp_path: Path) -> None:
    csv_path = tmp_path / "AP_History_2026-05-29.csv"
    csv_path.write_text(SAMPLE_CSV)

    df = read_csv(csv_path)

    open_count = (df["open"] == True).sum()
    closed_count = (df["open"] == False).sum()
    assert open_count == 1  # only the unpaid A2 invoice
    assert closed_count == 8


def test_read_csv_preserves_future_dated_rows(tmp_path: Path) -> None:
    """Pre-scheduled payments and book-entry pairs with future dates flow through.

    The extract does NOT filter these out -- the calc layer is responsible
    for bounding the trailing window. payments_timing.py specifically reads
    the future-dated payments as deterministic disbursements.
    """
    csv_path = tmp_path / "AP_History_2026-05-29.csv"
    csv_path.write_text(SAMPLE_CSV)

    df = read_csv(csv_path)

    as_of = dt.date(2026, 5, 29)
    future_rows = df[df["postingDate"] > as_of]
    assert len(future_rows) == 3

    pre_sched = future_rows[future_rows["documentType"] == "Payment"]
    assert len(pre_sched) == 1
    assert pre_sched["postingDate"].iloc[0] == dt.date(2026, 6, 1)
    assert pre_sched["vendorNumber"].iloc[0] == "VEND-B"
    assert pre_sched["amount"].iloc[0] == 75000

    book = future_rows[future_rows["postingDate"] == dt.date(2028, 10, 28)]
    assert len(book) == 2
    assert book["amount"].sum() == 0


def test_find_latest_csv_picks_most_recent_date(tmp_path: Path) -> None:
    (tmp_path / "AP_History_2026-05-27.csv").write_text(SAMPLE_CSV)
    (tmp_path / "AP_History_2026-05-29.csv").write_text(SAMPLE_CSV)
    (tmp_path / "AP_History_2026-05-28.csv").write_text(SAMPLE_CSV)

    latest = find_latest_csv(tmp_path)

    assert latest is not None
    assert latest.name == "AP_History_2026-05-29.csv"


def test_find_latest_csv_ignores_other_flow_outputs(tmp_path: Path) -> None:
    """AR_History, AP_vendorLedgerEntries, VendorMaster all ignored."""
    (tmp_path / "AP_History_2026-05-29.csv").write_text(SAMPLE_CSV)
    (tmp_path / "AR_History_2026-05-29.csv").write_text(SAMPLE_CSV)
    (tmp_path / "AP_vendorLedgerEntries_2026-05-29.csv").write_text(SAMPLE_CSV)
    (tmp_path / "VendorMaster_2026-05-29.csv").write_text("a,b\n1,2\n")
    (tmp_path / "PaymentTerms_2026-05-29.csv").write_text("a,b\n1,2\n")

    latest = find_latest_csv(tmp_path)

    assert latest is not None
    assert latest.name == "AP_History_2026-05-29.csv"


def test_find_latest_csv_returns_none_when_no_match(tmp_path: Path) -> None:
    (tmp_path / "AR_History_2026-05-29.csv").write_text(SAMPLE_CSV)
    (tmp_path / "other.csv").write_text("a,b\n1,2\n")

    assert find_latest_csv(tmp_path) is None