"""Extract the BC vendor master from OneDrive CSV into SQLite.

Reads the most recent VendorMaster_*.csv from the configured OneDrive folder,
selects the columns relevant to cash-flow forecasting (dropping Power Automate
serialization noise such as @odata.etag, ItemInternalId, the @odata.type
annotation columns, plus the address/contact/1099/tax columns we don't need),
parses dtypes, and writes the result into the bc_vendors table in SQLite.

This is the AP-side mirror of customer_master.py, with two design differences
worth knowing:

  1. The standard v2.0 vendors endpoint emits more columns than the customers
     endpoint (full address fields, phone, email, website, 1099 code, tax
     liability, payment method) -- 25 columns vs the customers' 14. None of
     the extra fields are relevant for cash forecasting, so usecols drops them.

  2. The `blocked` enum on vendors arrives encoded as `_x0020_` for the
     "unblocked" (blank) state -- Power Automate's XML-style encoding of a
     literal space character. read_csv normalises `_x0020_` to pd.NA so
     downstream consumers can do `df['blocked'].isna()` to find unblocked
     vendors. Real blocked states ("Payment", "All") flow through unchanged.

The fields we keep mirror the customer master's join keys plus the AP-side
balance field:
  - `number`  : vendor code (e.g. "1001") that joins to vendorNumber on the AP ledger
  - `paymentTermsId` : GUID that joins to bc_payment_terms.id (same shared
    paymentTerms table that customers reference)
  - `balance` : the vendor's master AP balance, analog of customer balanceDue,
    presented as a positive number in BC's master view despite the underlying
    ledger entries carrying negative amounts (the AP-side sign convention --
    see ap_ledger.py module docstring)
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

# 8 columns kept out of the 25 emitted by the v2.0 vendors endpoint. The other
# 17 (address/contact/tax/1099/payment method/etag/odata-type) are dropped via
# usecols. `number` is the join key to vendorNumber on the AP ledger.
# `paymentTermsId` joins to the shared bc_payment_terms.id (same table the
# customer master references). `balance` is the vendor's AP-balance FlowField,
# presented positive in the master despite the negative-amount ledger
# convention.
EXPECTED_COLUMNS = {
    "id": "string",
    "number": "string",
    "displayName": "string",
    "currencyCode": "string",
    "paymentTermsId": "string",
    "blocked": "string",
    "balance": "Float64",
    "lastModifiedDateTime": "string",
}

FILENAME_PATTERN = re.compile(r"^VendorMaster_(\d{4}-\d{2}-\d{2})\.csv$")

TABLE_NAME = "bc_vendors"

# Power Automate serializes a literal-space enum value as "_x0020_" via XML
# entity encoding. On the vendors endpoint this is what an unblocked vendor's
# `blocked` field carries; we normalize it to NA for cleaner downstream use.
_X0020_SENTINEL = "_x0020_"


def find_latest_csv(folder: Path) -> Optional[Path]:
    """Return the most recent VendorMaster_*.csv in folder, or None."""
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
    """Read the CSV, keep only EXPECTED_COLUMNS, parse datetime, normalize blocked.

    usecols both drops the serialization-noise/irrelevant columns and validates
    the schema: pandas raises if any expected column is absent from the file.

    The `_x0020_` sentinel in the blocked column is normalized to pd.NA so that
    downstream code can use a single .isna() check rather than a string compare
    plus null check.
    """
    df = pd.read_csv(path, usecols=list(EXPECTED_COLUMNS), dtype=EXPECTED_COLUMNS)
    df["lastModifiedDateTime"] = pd.to_datetime(
        df["lastModifiedDateTime"], format="ISO8601"
    )
    df["blocked"] = df["blocked"].replace(_X0020_SENTINEL, pd.NA)
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
        logger.error("No VendorMaster_*.csv found in %s", ONEDRIVE_DATA_PATH)
        return
    logger.info("Reading %s", csv_path)
    df = read_csv(csv_path)
    logger.info("Parsed %d rows", len(df))
    n = write_to_sqlite(df)
    logger.info("Wrote %d rows to SQLite table %s", n, TABLE_NAME)


if __name__ == "__main__":
    run()