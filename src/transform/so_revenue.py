"""Join open sales-order lines to headers and stamp an expected invoice date.

Reads bc_sales_order_headers + bc_sales_order_lines (from src.extract.so_data),
produces so_open_with_expected_invoice_date: one row per open SO line carrying
its customer, outstanding amount, and the date we expect it to convert to a
receivable.

EXPECTED INVOICE DATE = plannedShipmentDate (straight copy). Rationale: of the
candidate SO dates, plannedShipmentDate is the only consistently-populated,
forward-looking one. Header dates are unreliable (dummy/template SOs with stale
header dates); Shipment_Date is blank on open lines; Planned_Delivery_Date is a
stale original-plan date. A line invoices when it ships, so plannedShipmentDate
is the right timing field for converting committed orders into expected cash.

DEFENSIVE TYPE GUARD: Power Query pre-filters lines to Type == "Item", and the
column is retained for audit. Any non-"Item" row here is a PQ-filter regression:
we log an ERROR and drop those rows. The "Type" column has then served its
purpose and is dropped -- downstream modules never see it.

currencyCode is intentionally absent upstream and is not part of this layer.
"""
from __future__ import annotations

import logging

import pandas as pd

from src.config import LOG_LEVEL, LOG_FORMAT
from src.db import get_connection

logger = logging.getLogger(__name__)

HEADERS_TABLE = "bc_sales_order_headers"
LINES_TABLE = "bc_sales_order_lines"
OUTPUT_TABLE = "so_open_with_expected_invoice_date"

ITEM_TYPE = "Item"

OUTPUT_COLUMNS = [
    "soNumber", "lineNumber", "itemNumber", "description",
    "customerNumber", "customerName",
    "quantity", "quantityShipped", "outstandingQuantity",
    "unitPrice", "outstandingAmount",
    "plannedShipmentDate", "expected_invoice_date",
]


def build_so_revenue(headers: pd.DataFrame, lines: pd.DataFrame) -> pd.DataFrame:
    """Type-guard, inner-join lines to headers, filter to open qty, stamp dates.

    Returns the OUTPUT_COLUMNS shape (no "Type" column). Logs an ERROR if any
    non-Item rows are dropped, and an info count of lines dropped for having no
    matching header.
    """
    lines = lines.copy()

    # 1. Defensive Type guard: only Item rows are real product revenue.
    non_item = lines[lines["Type"] != ITEM_TYPE]
    if len(non_item) > 0:
        logger.error(
            "PQ filter regression: %d SO line(s) with Type != %r; dropping them. "
            "Offending types: %s",
            len(non_item), ITEM_TYPE, sorted(non_item["Type"].dropna().unique().tolist()),
        )
        lines = lines[lines["Type"] == ITEM_TYPE]
    # The Type column has served its purpose; downstream never sees it.
    lines = lines.drop(columns=["Type"])

    # 2. Inner join lines -> headers on soNumber (brings customer onto each line).
    n_before = len(lines)
    merged = lines.merge(
        headers[["soNumber", "customerNumber", "customerName"]],
        on="soNumber",
        how="inner",
    )
    dropped = n_before - len(merged)
    if dropped > 0:
        logger.info(
            "%d SO line(s) dropped: soNumber not found in headers (data-quality signal).",
            dropped,
        )

    # 3. Defensive open-quantity filter (PQ already does this; keep the guarantee).
    merged = merged[merged["outstandingQuantity"] > 0].copy()

    # 4. Stamp expected invoice date = plannedShipmentDate (see module docstring).
    merged["expected_invoice_date"] = merged["plannedShipmentDate"]

    # 5. Project to the output schema (no Type, no currencyCode).
    for col in OUTPUT_COLUMNS:
        if col not in merged.columns:
            merged[col] = pd.NA
    return merged[OUTPUT_COLUMNS]


def load_table(name: str) -> pd.DataFrame:
    """Read an entire SQLite table into a DataFrame."""
    with get_connection() as conn:
        return pd.read_sql(f"SELECT * FROM {name}", conn)


def write_to_sqlite(df: pd.DataFrame, table_name: str = OUTPUT_TABLE) -> int:
    """Replace the table contents with df. Returns the number of rows written."""
    with get_connection() as conn:
        df.to_sql(table_name, conn, if_exists="replace", index=False)
    return len(df)


def run() -> None:
    """Entrypoint: load SO tables, build the expected-invoice-date table, write."""
    logging.basicConfig(level=LOG_LEVEL, format=LOG_FORMAT)
    headers = load_table(HEADERS_TABLE)
    lines = load_table(LINES_TABLE)
    logger.info("Loaded %d SO headers, %d SO lines", len(headers), len(lines))

    out = build_so_revenue(headers, lines)
    n = write_to_sqlite(out)
    logger.info("Wrote %d rows to SQLite table %s", n, OUTPUT_TABLE)


if __name__ == "__main__":
    run()
