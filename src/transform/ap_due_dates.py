"""Derive invoice due dates for open AP entries.

Joins the three extracted tables in SQLite:
  bc_vendor_ledger_entries (open AP)  --vendorNumber-->  bc_vendors.number
  bc_vendors                          --paymentTermsId-->  bc_payment_terms.id

For each open AP entry it computes:
    dueDate = postingDate + <days implied by the vendor's payment term>

The AP-side mirror of src.transform.due_dates.py. The BC payment-terms table
is shared between customers and vendors (both reference paymentTermsId GUIDs
that resolve to the same bc_payment_terms.id rows), so this module reuses the
parse_due_date_calculation utility from the AR-side module rather than
re-implementing the BC formula parser. The entity-specific functions
(build_vendor_due_days, stamp_due_dates) mirror their AR-side counterparts
with vendorNumber substituted for customerNumber.

Sign-convention agnostic: the dueDate computation is postingDate + due_days
regardless of whether the underlying entry is an Invoice (negative amount on
AP side), Payment (positive), or Credit Memo (positive). Downstream
payments_timing.py decides what to do with each row type.

The result is written to ap_open_with_due_dates for the DPO and
payments-timing layers to consume.
"""
from __future__ import annotations

import logging

import pandas as pd

from src.config import LOG_LEVEL, LOG_FORMAT
from src.db import get_connection
from src.transform.due_dates import parse_due_date_calculation

logger = logging.getLogger(__name__)

# Vendor-side default. Same value as the AR-side default (NET30 is the
# overwhelmingly common term in BC tenants), defined as a fresh constant so
# the DPO module imports it from here rather than reaching through to
# due_dates.py for a customer-side default.
DEFAULT_DUE_DAYS = 30

OUTPUT_TABLE = "ap_open_with_due_dates"


def build_vendor_due_days(
    vendors: pd.DataFrame,
    terms: pd.DataFrame,
    default_days: int = DEFAULT_DUE_DAYS,
) -> dict[str, int]:
    """Build a {vendor number -> due days} lookup.

    Resolves each vendor's paymentTermsId through the shared bc_payment_terms
    table to a day count, falling back to default_days for blank/unknown terms
    or unparseable formulas.
    """
    term_days: dict[str, int] = {}
    for _, row in terms.iterrows():
        days = parse_due_date_calculation(row["dueDateCalculation"])
        term_days[row["id"]] = days if days is not None else default_days

    lookup: dict[str, int] = {}
    for _, row in vendors.iterrows():
        lookup[row["number"]] = term_days.get(row["paymentTermsId"], default_days)
    return lookup


def stamp_due_dates(
    ap: pd.DataFrame,
    vendor_due_days: dict[str, int],
    default_days: int = DEFAULT_DUE_DAYS,
) -> pd.DataFrame:
    """Add dueDays and dueDate columns to open AP entries.

    dueDate = postingDate + dueDays. Entries whose vendorNumber isn't in the
    lookup (shouldn't happen if the master is complete -- live data validates
    zero missing) fall back to default_days.
    """
    df = ap.copy()
    df["dueDays"] = (
        df["vendorNumber"].map(vendor_due_days).fillna(default_days).astype(int)
    )
    posting = pd.to_datetime(df["postingDate"])
    df["postingDate"] = posting.dt.date
    df["dueDate"] = (posting + pd.to_timedelta(df["dueDays"], unit="D")).dt.date
    return df


def load_table(name: str) -> pd.DataFrame:
    """Read an entire SQLite table into a DataFrame."""
    with get_connection() as conn:
        return pd.read_sql(f"SELECT * FROM {name}", conn)


def write_to_sqlite(df: pd.DataFrame, table_name: str = OUTPUT_TABLE) -> int:
    """Replace the table contents with df. Returns the number of rows written."""
    with get_connection() as conn:
        df.to_sql(table_name, conn, if_exists="replace", index=False)
    return len(df)


def _open_mask(series: pd.Series) -> pd.Series:
    """True where the AP 'open' flag is set, robust to bool/int/str storage."""
    return series.map(lambda v: str(v).strip().lower() in {"1", "true"})


def run() -> None:
    """Entrypoint: load tables, derive due dates on open AP, write result."""
    logging.basicConfig(level=LOG_LEVEL, format=LOG_FORMAT)
    vendors = load_table("bc_vendors")
    terms = load_table("bc_payment_terms")
    ap = load_table("bc_vendor_ledger_entries")
    logger.info(
        "Loaded %d vendors, %d terms, %d ledger entries",
        len(vendors), len(terms), len(ap),
    )

    ap_open = ap[_open_mask(ap["open"])].copy()
    logger.info("Open AP entries: %d", len(ap_open))

    lookup = build_vendor_due_days(vendors, terms)
    stamped = stamp_due_dates(ap_open, lookup)

    matched = stamped["vendorNumber"].isin(lookup).sum()
    unmatched = len(stamped) - matched
    if unmatched:
        logger.warning(
            "%d open AP entries had no vendor-master match; defaulted to %d days",
            unmatched, DEFAULT_DUE_DAYS,
        )

    n = write_to_sqlite(stamped)
    logger.info("Wrote %d rows to SQLite table %s", n, OUTPUT_TABLE)


if __name__ == "__main__":
    run()