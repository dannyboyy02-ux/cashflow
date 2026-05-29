"""Extract BC customer ledger history (12-month, all document types) into SQLite.

Reads the most recent AR_History_*.csv from the configured OneDrive folder,
parses it with explicit dtypes, validates the schema, and writes the result
into the bc_ar_history table in SQLite.

This is the companion to ar_ledger.py. Both pull from the same BC entity
(reportsFinance/beta customerLedgerEntries), so the column schema is identical.
The difference is upstream scope:
  - ar_ledger.py reads Flow 1's open-only snapshot (current AR balance).
  - ar_history.py reads Flow BC_AR_History's trailing 12-month window across
    all document types (Invoice, Credit Memo, Payment, Refund, plus rare
    blank-documentType journal entries). The history table is the DSO sales
    history -- the downstream calc filters to Invoice + Credit Memo for net
    credit sales by customer by month, and uses Payment / Refund rows for
    reconciliation only.
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

# Same 21-column shape as ar_ledger.py -- BC customerLedgerEntry surface plus
# Power Automate's @odata-style noise columns (ItemInternalId here, no
# @odata.etag in the reportsFinance/beta serialization, per the Day 1 run).
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
    "customerNumber": "string",
    "open": "string",
    "dimensionSetID": "Int64",
    "currencyCode": "string",
    "yourReference": "string",
    "lastModifiedDateTime": "string",
    "amount": "Float64",
    "debitAmount": "Float64",
    "creditAmount": "Float64",
    "amountLocalCurrency": "Float64",
    "debitAmountLocalCurrency": "Float64",
    "creditAmountLocalCurrency": "Float64",
}

FILENAME_PATTERN = re.compile(r"^AR_History_(\d{4}-\d{2}-\d{2})\.csv$")

TABLE_NAME = "bc_ar_history"


def find_latest_csv(folder: Path) -> Optional[Path]:
    """Return the most recent AR_History_*.csv in folder, or None."""
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

    The history pull spans all document types and both open/closed states, so
    the `open` column carries true/false rather than the uniform-true of Flow 1.
    Rare blank-documentType rows (manual journal entries) flow through as
    pd.NA in the string column and are filtered downstream in the DSO calc.
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
        logger.error("No AR_History_*.csv found in %s", ONEDRIVE_DATA_PATH)
        return
    logger.info("Reading %s", csv_path)
    df = read_csv(csv_path)
    logger.info("Parsed %d rows", len(df))
    n = write_to_sqlite(df)
    logger.info("Wrote %d rows to SQLite table %s", n, TABLE_NAME)


if __name__ == "__main__":
    run()