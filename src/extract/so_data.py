"""Extract open-sales-order data from the BC_SalesOrderData.xlsx workbook.

Unlike every other extract (Power-Automate-generated dated CSVs), the sales-order
data arrives as a single Excel workbook with two Power Query tables that Daniel
refreshes by opening the file each morning. Fixed filename, no date stamp:

    {ONEDRIVE_DATA_PATH}\\BC_SalesOrderData.xlsx

This ONE module reads both sheets:
  - "SalesOrders"      (headers) -> bc_sales_order_headers
  - "SalesOrderLines"  (lines)   -> bc_sales_order_lines

WHY THIS LAYER EXISTS: the 13-week forecast goes flat after ~week 6 because open
AR is only already-issued invoices. Open sales orders are committed-but-not-yet-
invoiced revenue; timing them by plannedShipmentDate fills weeks 4-13 with real
pipeline.

SCHEMA NOTE: the line column "Type" keeps its original BC capitalization (capital
T) while every other line column is lowerCamelCase. It is uniformly "Item"
(Power Query pre-filtered) and retained only for audit; the transform layer
guards on it. currencyCode is intentionally NOT present on the headers sheet and
is not carried by this layer.

GRACEFUL DEGRADATION (the pipeline must never break on SO data):
  - File missing (not refreshed yet): log WARNING, write empty-but-correctly-
    shaped tables, let the rest of the pipeline run AR-only.
  - File present but last modified > STALE_HOURS ago: log WARNING with the
    mtime so staleness is visible, then continue.
A column-name regression (PQ rename drift) is surfaced LOUDLY: read_* raises
ValueError listing the missing columns rather than silently coping.
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

WORKBOOK_NAME = "BC_SalesOrderData.xlsx"
HEADERS_SHEET = "SalesOrders"
LINES_SHEET = "SalesOrderLines"

HEADERS_TABLE = "bc_sales_order_headers"
LINES_TABLE = "bc_sales_order_lines"

STALE_HOURS = 25

# Headers sheet: currencyCode is intentionally excluded upstream, so it is not
# expected here.
HEADER_DTYPES = {
    "soNumber": "string",
    "customerNumber": "string",
    "customerName": "string",
    "status": "string",
}
HEADER_COLUMNS = list(HEADER_DTYPES.keys())

# Lines sheet: "Type" keeps its capital-T BC name; plannedShipmentDate is parsed
# to dt.date separately (not in the dtype map).
LINE_DTYPES = {
    "soNumber": "string",
    "lineNumber": "string",
    "Type": "string",
    "itemNumber": "string",
    "description": "string",
    "quantity": "Float64",
    "unitPrice": "Float64",
    "quantityShipped": "Float64",
    "outstandingQuantity": "Float64",
    "outstandingAmount": "Float64",
}
LINE_COLUMNS = [
    "soNumber", "lineNumber", "Type", "itemNumber", "description",
    "quantity", "unitPrice", "quantityShipped", "plannedShipmentDate",
    "outstandingQuantity", "outstandingAmount",
]


def find_workbook() -> Optional[Path]:
    """Return the path to BC_SalesOrderData.xlsx under ONEDRIVE_DATA_PATH, or None."""
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


def read_so_headers(path: Path) -> pd.DataFrame:
    """Read the SalesOrders sheet with explicit dtypes; empty-but-typed if no rows."""
    df = pd.read_excel(path, sheet_name=HEADERS_SHEET, dtype=HEADER_DTYPES, engine="openpyxl")
    _validate_columns(df, HEADER_COLUMNS, HEADERS_SHEET)
    return df[HEADER_COLUMNS]


def read_so_lines(path: Path) -> pd.DataFrame:
    """Read the SalesOrderLines sheet with explicit dtypes.

    plannedShipmentDate is coerced to dt.date (not datetime) to match the rest of
    the pipeline's date handling. The capital-T "Type" column is preserved.
    """
    df = pd.read_excel(path, sheet_name=LINES_SHEET, dtype=LINE_DTYPES, engine="openpyxl")
    _validate_columns(df, LINE_COLUMNS, LINES_SHEET)
    df["plannedShipmentDate"] = pd.to_datetime(df["plannedShipmentDate"]).dt.date
    return df[LINE_COLUMNS]


def _empty_headers() -> pd.DataFrame:
    return pd.DataFrame({c: pd.Series(dtype=HEADER_DTYPES[c]) for c in HEADER_COLUMNS})


def _empty_lines() -> pd.DataFrame:
    cols = {c: pd.Series(dtype=LINE_DTYPES.get(c, "object")) for c in LINE_COLUMNS}
    return pd.DataFrame(cols)


def write_to_sqlite(headers_df: pd.DataFrame, lines_df: pd.DataFrame) -> tuple[int, int]:
    """Replace both SO tables. Returns (header_rows, line_rows)."""
    with get_connection() as conn:
        headers_df.to_sql(HEADERS_TABLE, conn, if_exists="replace", index=False)
        lines_df.to_sql(LINES_TABLE, conn, if_exists="replace", index=False)
    return len(headers_df), len(lines_df)


def run() -> None:
    """Entrypoint: locate the workbook, check staleness, read both sheets, write.

    On a missing file the SO tables are written empty so the rest of the pipeline
    runs AR-only; a one-line summary (mtime, header count, line count) is logged.
    """
    logging.basicConfig(level=LOG_LEVEL, format=LOG_FORMAT)
    path = find_workbook()
    if path is None:
        logger.warning(
            "%s not found under %s; writing empty SO tables -- forecast has no "
            "open-SO layer today.", WORKBOOK_NAME, ONEDRIVE_DATA_PATH,
        )
        write_to_sqlite(_empty_headers(), _empty_lines())
        return

    mtime = dt.datetime.fromtimestamp(path.stat().st_mtime)
    age_hours = (dt.datetime.now() - mtime).total_seconds() / 3600.0
    if age_hours > STALE_HOURS:
        logger.warning(
            "%s is stale: last modified %s (%.1f h ago, > %d h threshold). "
            "Refresh the workbook in Excel to update the open-SO layer.",
            WORKBOOK_NAME, mtime.isoformat(timespec="seconds"), age_hours, STALE_HOURS,
        )

    headers = read_so_headers(path)
    lines = read_so_lines(path)
    n_headers, n_lines = write_to_sqlite(headers, lines)
    logger.info(
        "SO extract: mtime=%s, %d headers -> %s, %d lines -> %s",
        mtime.isoformat(timespec="seconds"), n_headers, HEADERS_TABLE, n_lines, LINES_TABLE,
    )


if __name__ == "__main__":
    run()
