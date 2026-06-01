"""Join open PO lines to their headers, carrying vendor + status onto each line.

The PO-side analogue of src.transform.so_revenue. Inner-joins
bc_purchase_order_lines to bc_purchase_order_headers on poNumber and carries
vendorNumber, vendorName, Status, and documentDate from the header onto each
line. Output table: po_open_lines (one row per open PO line), consumed by
src.calc.po_payments_timing.

The lines sheet is already filtered upstream (outstandingQuantity > 0 OR
rbniQuantity > 0); this module does not re-filter, it only enriches with header
fields. Lines whose poNumber has no matching header are dropped by the inner
join and counted as a data-quality signal.
"""
from __future__ import annotations

import logging

import pandas as pd

from src.config import LOG_LEVEL, LOG_FORMAT
from src.db import get_connection

logger = logging.getLogger(__name__)

HEADERS_TABLE = "bc_purchase_order_headers"
LINES_TABLE = "bc_purchase_order_lines"
OUTPUT_TABLE = "po_open_lines"

HEADER_CARRY_COLUMNS = ["poNumber", "vendorNumber", "vendorName", "Status", "documentDate"]


def build_po_open_lines(headers: pd.DataFrame, lines: pd.DataFrame) -> pd.DataFrame:
    """Inner-join lines to headers on poNumber, carrying header fields onto lines."""
    n_before = len(lines)
    merged = lines.merge(
        headers[HEADER_CARRY_COLUMNS],
        on="poNumber",
        how="inner",
    )
    dropped = n_before - len(merged)
    if dropped > 0:
        logger.info(
            "%d PO line(s) dropped: poNumber not found in headers (data-quality signal).",
            dropped,
        )
    return merged


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
    """Entrypoint: load PO tables, enrich lines with header fields, write result."""
    logging.basicConfig(level=LOG_LEVEL, format=LOG_FORMAT)
    headers = load_table(HEADERS_TABLE)
    lines = load_table(LINES_TABLE)
    logger.info("Loaded %d PO headers, %d PO lines", len(headers), len(lines))

    out = build_po_open_lines(headers, lines)
    n = write_to_sqlite(out)
    logger.info("Wrote %d rows to SQLite table %s", n, OUTPUT_TABLE)


if __name__ == "__main__":
    run()
