"""Extract open purchase-order data from the BC_PurchaseOrderData.xlsx workbook.

The AP-side analogue of src.extract.so_data: a single Power Query workbook
(refresh-on-open) with two sheets that Daniel refreshes each morning. Fixed
filename, no date stamp:

    {ONEDRIVE_DATA_PATH}\\BC_PurchaseOrderData.xlsx

This ONE module reads both sheets:
  - "PurchaseOrders"      (headers) -> bc_purchase_order_headers
  - "PurchaseOrderLines"  (lines)   -> bc_purchase_order_lines

WHY THIS LAYER EXISTS: the 13-week AP disbursement forecast is missing two
future-cash streams that open PO data carries: RBNI (received but not invoiced --
goods in hand, no AP entry yet) and PO outstanding (ordered, not yet received).
The Phase 5 calc layer turns these into timed disbursements.

SCHEMA NOTES (follow the file):
  - Several line columns keep original BC underscore captions (Qty_to_Invoice,
    Qty_to_Receive, Line_Amount); they are retained for audit. The calc layer
    uses the renamed columns (rbniAmount, outstandingAmount, expectedReceiptDate).
  - Type carries multiple values (Item, Charge (Item), G/L Account, Resource,
    possibly Fixed Asset). We do NOT assert any particular set.
  - PurchaseOrders.vendorNumber arrives numeric (e.g. 1234.0) from the OData
    feed; bc_vendor_dpo / bc_vendors key on text vendor numbers ("1234"), so it
    is coerced to a clean integer-string here for the downstream DPO join.

GRACEFUL DEGRADATION (the pipeline must never break on PO data):
  - File missing: log WARNING, write empty-but-correctly-shaped tables, return.
  - File present but last modified > STALE_HOURS ago: log WARNING, then continue.
A column-name regression is surfaced loudly (read_* raises ValueError).
"""
from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path
from typing import Optional

import pandas as pd

from src.config import ONEDRIVE_DATA_PATH, LOG_LEVEL, LOG_FORMAT
from src.db import get_connection

logger = logging.getLogger(__name__)

WORKBOOK_NAME = "BC_PurchaseOrderData.xlsx"
HEADERS_SHEET = "PurchaseOrders"
LINES_SHEET = "PurchaseOrderLines"

HEADERS_TABLE = "bc_purchase_order_headers"
LINES_TABLE = "bc_purchase_order_lines"

STALE_HOURS = 25

HEADER_COLUMNS = ["poNumber", "vendorNumber", "vendorName", "documentDate", "Status"]

LINE_COLUMNS = [
    "expectedReceiptDate", "quantityInvoiced", "Qty_to_Invoice",
    "quantityReceived", "Qty_to_Receive", "Line_Amount", "unitCost",
    "quantity", "description", "itemNumber", "Type", "lineNumber",
    "poNumber", "outstandingQuantity", "rbniQuantity", "rbniAmount",
    "outstandingAmount",
]

# Text columns coerced to pandas string dtype on read.
_HEADER_TEXT = ["poNumber", "vendorName", "Status"]
_LINE_TEXT = ["poNumber", "description", "itemNumber", "Type", "lineNumber"]


def find_workbook() -> Optional[Path]:
    """Return the path to BC_PurchaseOrderData.xlsx under ONEDRIVE_DATA_PATH, or None."""
    path = ONEDRIVE_DATA_PATH / WORKBOOK_NAME
    return path if path.exists() else None


def _validate_columns(df: pd.DataFrame, expected: list[str], sheet: str) -> None:
    """Raise on missing columns (loud schema-regression surfacing); warn on extras."""
    missing = set(expected) - set(df.columns)
    if missing:
        raise ValueError(f"Sheet {sheet!r} missing expected columns: {sorted(missing)}")
    extra = set(df.columns) - set(expected)
    if extra:
        logger.warning("Sheet %r has unexpected columns ignored: %s", sheet, sorted(extra))


def _clean_vendor_number(s: pd.Series) -> pd.Series:
    """Coerce a numeric-looking vendor number (1234.0) to a clean string ('1234').

    Non-numeric vendor codes (none in current data, but possible) pass through as
    their stripped string form.
    """
    num = pd.to_numeric(s, errors="coerce")
    as_int = num.astype("Int64").astype("string")
    original = s.astype("string").str.strip()
    return as_int.where(num.notna(), original)


def read_po_headers(path: Path) -> pd.DataFrame:
    """Read the PurchaseOrders sheet; coerce vendorNumber to string, dates to date."""
    df = pd.read_excel(path, sheet_name=HEADERS_SHEET, engine="openpyxl")
    _validate_columns(df, HEADER_COLUMNS, HEADERS_SHEET)
    df["vendorNumber"] = _clean_vendor_number(df["vendorNumber"])
    for c in _HEADER_TEXT:
        df[c] = df[c].astype("string")
    df["documentDate"] = pd.to_datetime(df["documentDate"]).dt.date
    return df[HEADER_COLUMNS]


def read_po_lines(path: Path) -> pd.DataFrame:
    """Read the PurchaseOrderLines sheet; expectedReceiptDate coerced to dt.date.

    Type carries multiple values and is preserved as-is (no uniformity assertion).
    """
    df = pd.read_excel(path, sheet_name=LINES_SHEET, engine="openpyxl")
    _validate_columns(df, LINE_COLUMNS, LINES_SHEET)
    df["expectedReceiptDate"] = pd.to_datetime(df["expectedReceiptDate"]).dt.date
    for c in _LINE_TEXT:
        df[c] = df[c].astype("string")
    return df[LINE_COLUMNS]


def _empty_headers() -> pd.DataFrame:
    return pd.DataFrame({c: pd.Series(dtype="object") for c in HEADER_COLUMNS})


def _empty_lines() -> pd.DataFrame:
    return pd.DataFrame({c: pd.Series(dtype="object") for c in LINE_COLUMNS})


def write_to_sqlite(headers_df: pd.DataFrame, lines_df: pd.DataFrame) -> tuple[int, int]:
    """Replace both PO tables. Returns (header_rows, line_rows)."""
    with get_connection() as conn:
        headers_df.to_sql(HEADERS_TABLE, conn, if_exists="replace", index=False)
        lines_df.to_sql(LINES_TABLE, conn, if_exists="replace", index=False)
    return len(headers_df), len(lines_df)


def run() -> None:
    """Entrypoint: locate the workbook, check staleness, read both sheets, write.

    On a missing file the PO tables are written empty so the rest of the pipeline
    runs without the PO layer; a one-line summary is logged.
    """
    logging.basicConfig(level=LOG_LEVEL, format=LOG_FORMAT)
    path = find_workbook()
    if path is None:
        logger.warning(
            "%s not found under %s; writing empty PO tables -- forecast has no "
            "open-PO layer today.", WORKBOOK_NAME, ONEDRIVE_DATA_PATH,
        )
        write_to_sqlite(_empty_headers(), _empty_lines())
        return

    mtime = dt.datetime.fromtimestamp(path.stat().st_mtime)
    age_hours = (dt.datetime.now() - mtime).total_seconds() / 3600.0
    if age_hours > STALE_HOURS:
        logger.warning(
            "%s is stale: last modified %s (%.1f h ago, > %d h threshold). "
            "Refresh the workbook in Excel to update the open-PO layer.",
            WORKBOOK_NAME, mtime.isoformat(timespec="seconds"), age_hours, STALE_HOURS,
        )

    headers = read_po_headers(path)
    lines = read_po_lines(path)
    n_headers, n_lines = write_to_sqlite(headers, lines)
    logger.info(
        "PO extract: mtime=%s, %d headers -> %s, %d lines -> %s",
        mtime.isoformat(timespec="seconds"), n_headers, HEADERS_TABLE, n_lines, LINES_TABLE,
    )


if __name__ == "__main__":
    run()
