"""Tests for src/extract/ap_ledger.py."""
from pathlib import Path

import pandas as pd
import pytest

from src.extract.ap_ledger import (
    EXPECTED_COLUMNS,
    find_latest_csv,
    read_csv,
)


# Realistic AP fixture drawn from the actual Flow 2 sanity-test JSON.
# Mirrors the customerLedgerEntry shape minus yourReference, with vendorNumber
# substituting for customerNumber. Critically: invoices have NEGATIVE amounts
# (AP-side sign convention), payments and credit memos have POSITIVE amounts.
SAMPLE_CSV = (
    "ItemInternalId,entryNumber,documentType,description,postingDate,"
    "documentNumber,externalDocumentNumber,balancingAccountNumber,"
    "balancingAccountType,vendorNumber,open,dimensionSetID,currencyCode,"
    "lastModifiedDateTime,amount,debitAmount,creditAmount,"
    "amountLocalCurrency,debitAmountLocalCurrency,creditAmountLocalCurrency\n"
    "32574410,32574410,Invoice,Invoice 13303,2023-10-08,13303,13303,21100,"
    "G/L Account,6322,false,0,,2023-10-17T00:50:32.517Z,-1034.78,0,1034.78,"
    "-1034.78,0,1034.78\n"
    "32574412,32574412,Invoice,Invoice 48507,2023-10-08,48507,48507,21100,"
    "G/L Account,1080,true,0,,2023-11-17T18:54:00.203Z,-1086,0,1086,-1086,"
    "0,1086\n"
    "32574999,32574999,Payment,Pmt to 6322,2026-04-15,PMT001,,21100,"
    "G/L Account,6322,true,0,,2026-04-15T10:00:00.000Z,500,500,0,500,500,0\n"
    "32575000,32575000,Credit Memo,CM from 6670,2026-05-01,CM001,,21100,"
    "G/L Account,6670,true,0,,2026-05-01T10:00:00.000Z,250,250,0,250,250,0\n"
)


def test_read_csv_returns_dataframe_with_expected_shape(tmp_path: Path) -> None:
    csv_path = tmp_path / "AP_vendorLedgerEntries_2026-05-29.csv"
    csv_path.write_text(SAMPLE_CSV)

    df = read_csv(csv_path)

    assert len(df) == 4
    assert set(df.columns) == set(EXPECTED_COLUMNS)
    assert "yourReference" not in df.columns
    assert "vendorNumber" in df.columns
    assert "customerNumber" not in df.columns


def test_read_csv_preserves_ap_sign_convention(tmp_path: Path) -> None:
    """AP invoices have negative amounts; payments/credit memos have positive.

    The extract preserves the raw signed amount as-is. The downstream calc
    layer is responsible for flipping signs to produce positive AP balances.
    """
    csv_path = tmp_path / "AP_vendorLedgerEntries_2026-05-29.csv"
    csv_path.write_text(SAMPLE_CSV)

    df = read_csv(csv_path)

    invoices = df[df["documentType"] == "Invoice"]
    assert (invoices["amount"] < 0).all()
    payments = df[df["documentType"] == "Payment"]
    assert (payments["amount"] > 0).all()
    credit_memos = df[df["documentType"] == "Credit Memo"]
    assert (credit_memos["amount"] > 0).all()


def test_read_csv_ap_balance_computation_matches_convention(tmp_path: Path) -> None:
    """The documented `-sum(amount where open=true)` produces correct AP balances.

    Vendor 1080: one open invoice of -1086. AP balance = -(-1086) = 1086. (we owe)
    Vendor 6322: one open payment of +500 (no open invoice). AP balance = -500.
                 (negative = unapplied credit sitting against the vendor)
    Vendor 6670: one open credit memo of +250 (no open invoice). AP balance = -250.
                 (negative = unapplied credit memo sitting against the vendor)
    """
    csv_path = tmp_path / "AP_vendorLedgerEntries_2026-05-29.csv"
    csv_path.write_text(SAMPLE_CSV)

    df = read_csv(csv_path)

    open_rows = df[df["open"] == True]
    ap_balance = -open_rows.groupby("vendorNumber")["amount"].sum()
    assert ap_balance["1080"] == pytest.approx(1086.0)
    assert ap_balance["6322"] == pytest.approx(-500.0)
    assert ap_balance["6670"] == pytest.approx(-250.0)


def test_find_latest_csv_picks_most_recent_date(tmp_path: Path) -> None:
    (tmp_path / "AP_vendorLedgerEntries_2026-05-27.csv").write_text(SAMPLE_CSV)
    (tmp_path / "AP_vendorLedgerEntries_2026-05-29.csv").write_text(SAMPLE_CSV)
    (tmp_path / "AP_vendorLedgerEntries_2026-05-28.csv").write_text(SAMPLE_CSV)

    latest = find_latest_csv(tmp_path)

    assert latest is not None
    assert latest.name == "AP_vendorLedgerEntries_2026-05-29.csv"


def test_find_latest_csv_ignores_other_flow_outputs(tmp_path: Path) -> None:
    """AR_*, AR_History_*, CustomerMaster_*, PaymentTerms_* should all be ignored."""
    (tmp_path / "AP_vendorLedgerEntries_2026-05-29.csv").write_text(SAMPLE_CSV)
    (tmp_path / "AR_customerLedgerEntries_2026-05-29.csv").write_text(SAMPLE_CSV)
    (tmp_path / "AR_History_2026-05-29.csv").write_text(SAMPLE_CSV)
    (tmp_path / "CustomerMaster_2026-05-29.csv").write_text("a,b\n1,2\n")
    (tmp_path / "PaymentTerms_2026-05-29.csv").write_text("a,b\n1,2\n")

    latest = find_latest_csv(tmp_path)

    assert latest is not None
    assert latest.name == "AP_vendorLedgerEntries_2026-05-29.csv"


def test_find_latest_csv_returns_none_when_no_match(tmp_path: Path) -> None:
    (tmp_path / "AR_customerLedgerEntries_2026-05-29.csv").write_text(SAMPLE_CSV)
    (tmp_path / "other.csv").write_text("a,b\n1,2\n")

    assert find_latest_csv(tmp_path) is None