"""Extract BC vendor ledger history (12-month, all document types) into SQLite.

Reads the most recent AP_History_*.csv from the configured OneDrive folder,
parses it with explicit dtypes, validates the schema, and writes the result
into the bc_ap_history table in SQLite.

This is the AP-side companion to ar_history.py and the wider-scope sibling of
ap_ledger.py:
  - ap_ledger.py reads Flow 2's open-only snapshot (current AP balance).
  - ap_history.py reads the BC_AP_History flow's trailing 12-month window
    across all document types (Invoice, Credit Memo, Payment, Refund, plus
    occasional blank-documentType journal entries). The history table is the
    DPO purchase-basis history -- the downstream DPO calc filters to Invoice
    + Credit Memo for trailing net purchases per vendor.

SIGN CONVENTION (same as ap_ledger.py, inverted from the AR side):

Invoices carry NEGATIVE `amount` values (AP-side credit, we owe more).
Payments and Credit Memos carry POSITIVE values (debit AP, we owe less).
The extract preserves the raw signed amount; the calc layer flips signs to
get positive trailing-net-purchases and positive AP balances. See ap_ledger.py
for the full sign-convention note.

DATE-RANGE NOTE -- worth knowing:

The upstream PA filter is `postingDate ge {12mo ago}`, a one-sided lower
bound. BC tenants commonly contain entries with future-dated postingDate
values for two reasons:

  1. Pre-scheduled payments. AP teams enter the next-Monday payment run with
     postingDate set to that Monday and apply each payment to its target
     invoices in advance. The payment then closes (open=false) while sitting
     with a future postingDate until the actual cash leaves the bank. These
     are real future disbursements and matter for the cash forecast --
     payments_timing.py reads them directly from this table.
  2. Reversing book pairs. Intercompany/lease accrual workflows occasionally
     post invoice + credit memo pairs to far-future dates (e.g. 2028, 2036)
     that net to zero. These should be excluded from the DPO purchase basis
     and ignored for cash forecasting.

The DPO calc must therefore bound the trailing window on BOTH sides:
`as_of - 365 <= postingDate <= as_of`. The one-sided PA filter alone is not
sufficient.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

import pandas as pd

from src.config import ONEDRIVE_DATA_PATH, LOG_LEVEL, LOG_FORMAT
from src.db import get_connection

logger = logging.getLogger(__name__)

# Same 20-column shape as ap_ledger.py -- BC vendorLedgerEntry surface plus
# Power Automate's ItemInternalId. No yourReference (vendor side lacks it).
EXPECTED_COLUMNS = {
    "ItemInternalId": "string",
    "entryNumber": "Int64",
    "documentType": "string",
    "description": "string",
    "postingDate": "string",
    "documentNumber": "string",
    "externalDocumentNumber": "string",
    "balancingAccountNumber": "string",
    "balancingAccountType": "string",
    "vendorNumber": "string",
    "open": "string",
    "dimensionSetID": "Int64",
    "currencyCode": "string",
    "lastModifiedDateTime": "string",
    "amount": "Float64",
    "debitAmount": "Float64",
    "creditAmount": "Float64",
    "amountLocalCurrency": "Float64",
    "debitAmountLocalCurrency": "Float64",
    "creditAmountLocalCurrency": "Float64",
}

FILENAME_PATTERN = re.compile(r"^AP_History_(\d{4}-\d{2}-\d{2})\.csv$")

TABLE_NAME = "bc_ap_history"


def find_latest_csv(folder: Path) -> Optional[Path]:
    """Return the most recent AP_History_*.csv in folder, or None."""
    if not folder.exists():
        raise FileNotFoundError(f"OneDrive folder not found: {folder}")
    candidates = []
    for p in folder.iterdir():
        m = FILENAME_PATTERN.match(p.name)
        if m:
            candidates.append((m.group(1), p))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


def read_csv(path: Path) -> pd.DataFrame:
    """Read the CSV with explicit dtypes and parse date / datetime / bool columns.

    The history pull spans all document types and both open/closed states,
    and may include future-dated rows (pre-scheduled payments + reversing
    book pairs -- see module docstring). Rare blank-documentType rows
    (manual journal entries) flow through as pd.NA in the string column
    and are filtered downstream in the DPO calc.
    """
    df = pd.read_csv(path, dtype=EXPECTED_COLUMNS)

    missing = set(EXPECTED_COLUMNS) - set(df.columns)
    if missing:
        raise ValueError(f"Missing expected columns: {missing}")
    extra = set(df.columns) - set(EXPECTED_COLUMNS)
    if extra:
        logger.warning("Unexpected columns ignored: %s", extra)

    df["postingDate"] = pd.to_datetime(df["postingDate"], format="%Y-%m-%d").dt.date
    df["lastModifiedDateTime"] = pd.to_datetime(df["lastModifiedDateTime"], format="ISO8601")
    df["open"] = df["open"].str.lower().map({"true": True, "false": False})

    return df


def write_to_sqlite(df: pd.DataFrame, table_name: str = TABLE_NAME) -> int:
    """Replace the table contents with df. Returns the number of rows written."""
    with get_connection() as conn:
        df.to_sql(table_name, conn, if_exists="replace", index=False)
    return len(df)


def run() -> None:
    """Entrypoint: find the latest CSV, parse it, write to SQLite."""
    logging.basicConfig(level=LOG_LEVEL, format=LOG_FORMAT)
    logger.info("OneDrive folder: %s", ONEDRIVE_DATA_PATH)
    csv_path = find_latest_csv(ONEDRIVE_DATA_PATH)
    if csv_path is None:
        logger.error("No AP_History_*.csv found in %s", ONEDRIVE_DATA_PATH)
        return
    logger.info("Reading %s", csv_path)
    df = read_csv(csv_path)
    logger.info("Parsed %d rows", len(df))
    n = write_to_sqlite(df)
    logger.info("Wrote %d rows to SQLite table %s", n, TABLE_NAME)


if __name__ == "__main__":
    run()