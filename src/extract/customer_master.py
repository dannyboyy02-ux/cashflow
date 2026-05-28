"""Extract the BC customer master from OneDrive CSV into SQLite.

Reads the most recent CustomerMaster_*.csv from the configured OneDrive folder,
selects the columns relevant to cash-flow forecasting (dropping Power Automate
serialization noise such as @odata.etag, ItemInternalId, and the enum
@odata.type annotation columns), parses dtypes, and writes the result into the
bc_customers table in SQLite.

The standard v2.0 customers endpoint emits more columns than we need; unlike the
reportsFinance/beta ledger feeds, it also emits @odata.etag and enum
@odata.type partner columns. We therefore select with usecols rather than
reading every column.
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

# Columns we keep, mapped to pandas dtypes. Everything else in the CSV
# (@odata.etag, ItemInternalId, type@odata.type, type, address/contact fields,
# tax fields, shipment/payment method, blocked@odata.type) is intentionally
# dropped via usecols. `number` is the customer code (e.g. "CUST-B") that joins
# to customerNumber on the AR ledger. `paymentTermsId` is the GUID that joins to
# bc_payment_terms.id for due-date derivation.
EXPECTED_COLUMNS = {
    "id": "string",
    "number": "string",
    "displayName": "string",
    "currencyCode": "string",
    "paymentTermsId": "string",
    "blocked": "string",
    "balanceDue": "Float64",
    "creditLimit": "Float64",
    "lastModifiedDateTime": "string",
}

FILENAME_PATTERN = re.compile(r"^CustomerMaster_(\d{4}-\d{2}-\d{2})\.csv$")

TABLE_NAME = "bc_customers"


def find_latest_csv(folder: Path) -> Optional[Path]:
    """Return the most recent CustomerMaster_*.csv in folder, or None."""
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
    """Read the CSV, keeping only EXPECTED_COLUMNS, and parse the datetime column.

    usecols both drops the serialization-noise columns and validates the schema:
    pandas raises if any expected column is absent from the file.
    """
    df = pd.read_csv(path, usecols=list(EXPECTED_COLUMNS), dtype=EXPECTED_COLUMNS)
    df["lastModifiedDateTime"] = pd.to_datetime(
        df["lastModifiedDateTime"], format="ISO8601"
    )
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
        logger.error("No CustomerMaster_*.csv found in %s", ONEDRIVE_DATA_PATH)
        return
    logger.info("Reading %s", csv_path)
    df = read_csv(csv_path)
    logger.info("Parsed %d rows", len(df))
    n = write_to_sqlite(df)
    logger.info("Wrote %d rows to SQLite table %s", n, TABLE_NAME)


if __name__ == "__main__":
    run()