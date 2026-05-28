"""Extract BC payment terms from OneDrive CSV into SQLite.

Reads the most recent PaymentTerms_*.csv from the configured OneDrive folder,
keeps the relevant columns (dropping @odata.etag and ItemInternalId noise),
parses dtypes, and writes the result into the bc_payment_terms table in SQLite.
paymentTerms has no enum fields, so there are no @odata.type partner columns.

The dueDateCalculation column holds a BC date formula (e.g. "30D"); it is stored
verbatim here and parsed into a day count downstream in the transform layer.
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

# `id` is the GUID that customers.paymentTermsId points at. dueDateCalculation
# is the formula string the transform parses. discountPercent is read as a
# nullable float; calculateDiscountOnCreditMemos arrives as "TRUE"/"FALSE" text
# and is kept as a string (not needed for the cash forecast, retained for audit).
EXPECTED_COLUMNS = {
    "id": "string",
    "code": "string",
    "displayName": "string",
    "dueDateCalculation": "string",
    "discountDateCalculation": "string",
    "discountPercent": "Float64",
    "calculateDiscountOnCreditMemos": "string",
    "lastModifiedDateTime": "string",
}

FILENAME_PATTERN = re.compile(r"^PaymentTerms_(\d{4}-\d{2}-\d{2})\.csv$")

TABLE_NAME = "bc_payment_terms"


def find_latest_csv(folder: Path) -> Optional[Path]:
    """Return the most recent PaymentTerms_*.csv in folder, or None."""
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
    """Read the CSV, keeping only EXPECTED_COLUMNS, and parse the datetime column."""
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
        logger.error("No PaymentTerms_*.csv found in %s", ONEDRIVE_DATA_PATH)
        return
    logger.info("Reading %s", csv_path)
    df = read_csv(csv_path)
    logger.info("Parsed %d rows", len(df))
    n = write_to_sqlite(df)
    logger.info("Wrote %d rows to SQLite table %s", n, TABLE_NAME)


if __name__ == "__main__":
    run()