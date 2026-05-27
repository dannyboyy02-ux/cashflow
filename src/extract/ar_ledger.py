"""Extract AR customer ledger entries from OneDrive CSV into SQLite.

Reads the most recent AR_customerLedgerEntries_*.csv from the configured
OneDrive folder, parses it with explicit dtypes, validates the schema, and
writes the result into the bc_customer_ledger_entries table in SQLite.
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

FILENAME_PATTERN = re.compile(r"^AR_customerLedgerEntries_(\d{4}-\d{2}-\d{2})\.csv$")

TABLE_NAME = "bc_customer_ledger_entries"


def find_latest_csv(folder: Path) -> Optional[Path]:
    """Return the most recent AR_customerLedgerEntries_*.csv in folder, or None."""
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
    """Read the CSV with explicit dtypes and parse date / datetime / bool columns."""
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
        logger.error("No AR_customerLedgerEntries_*.csv found in %s", ONEDRIVE_DATA_PATH)
        return
    logger.info("Reading %s", csv_path)
    df = read_csv(csv_path)
    logger.info("Parsed %d rows", len(df))
    n = write_to_sqlite(df)
    logger.info("Wrote %d rows to SQLite table %s", n, TABLE_NAME)


if __name__ == "__main__":
    run()