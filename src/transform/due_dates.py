"""Derive invoice due dates for open AR entries.

Joins the three extracted tables in SQLite:
  bc_customer_ledger_entries (open AR)  --customerNumber-->  bc_customers.number
  bc_customers                          --paymentTermsId-->  bc_payment_terms.id

For each open AR entry it computes:
    dueDate = postingDate + <days implied by the customer's payment term>

Payment terms store a BC date formula (e.g. "30D") in dueDateCalculation, which
is parsed into a day count. Customers with a blank payment term (the zero-GUID),
a term that isn't in the terms table, or a formula we can't parse fall back to
the house-standard NET30 (30 days) -- which covers ~91% of the customer base, so
it is both the literal default and the safest approximation.

The result is written to the ar_open_with_due_dates table for the calc layer to
bucket into forecast weeks.
"""
from __future__ import annotations

import logging
import re

import pandas as pd

from src.config import LOG_LEVEL, LOG_FORMAT
from src.db import get_connection

logger = logging.getLogger(__name__)

# House-standard NET30. Used when a customer's term is blank, unknown, or carries
# a formula we can't parse (e.g. a future calendar-month "CM" form).
DEFAULT_DUE_DAYS = 30

OUTPUT_TABLE = "ar_open_with_due_dates"

# BC date formulas seen in the data are all the simple <n><unit> shape. We parse
# D/W/M/Y; months and years are approximated in days (terms in this data are all
# day-based, so the approximation never actually triggers). Calendar-relative
# forms like "CM" or "CM+10D" deliberately fall through to None so the caller
# applies the default rather than silently mis-dating.
_FORMULA_PATTERN = re.compile(r"^(\d+)([DWMY])$")
_UNIT_DAYS = {"D": 1, "W": 7, "M": 30, "Y": 365}


def parse_due_date_calculation(formula) -> int | None:
    """Convert a BC date formula like '30D' into a number of days.

    Returns None for empty/missing values, calendar-relative forms (CM...), or
    anything else we can't express as a fixed day offset; the caller substitutes
    DEFAULT_DUE_DAYS in that case.
    """
    if formula is None:
        return None
    try:
        if pd.isna(formula):
            return None
    except (TypeError, ValueError):
        pass
    s = str(formula).strip().upper()
    if not s:
        return None
    m = _FORMULA_PATTERN.match(s)
    if not m:
        return None
    return int(m.group(1)) * _UNIT_DAYS[m.group(2)]


def build_customer_due_days(
    customers: pd.DataFrame,
    terms: pd.DataFrame,
    default_days: int = DEFAULT_DUE_DAYS,
) -> dict[str, int]:
    """Build a {customer number -> due days} lookup.

    Resolves each customer's paymentTermsId through the terms table to a day
    count, falling back to default_days for blank/unknown terms or unparseable
    formulas.
    """
    term_days: dict[str, int] = {}
    for _, row in terms.iterrows():
        days = parse_due_date_calculation(row["dueDateCalculation"])
        term_days[row["id"]] = days if days is not None else default_days

    lookup: dict[str, int] = {}
    for _, row in customers.iterrows():
        lookup[row["number"]] = term_days.get(row["paymentTermsId"], default_days)
    return lookup


def stamp_due_dates(
    ar: pd.DataFrame,
    customer_due_days: dict[str, int],
    default_days: int = DEFAULT_DUE_DAYS,
) -> pd.DataFrame:
    """Add dueDays and dueDate columns to open AR entries.

    dueDate = postingDate + dueDays. Entries whose customerNumber isn't in the
    lookup (shouldn't happen if the master is complete) fall back to default_days.
    """
    df = ar.copy()
    df["dueDays"] = (
        df["customerNumber"].map(customer_due_days).fillna(default_days).astype(int)
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
    """True where the AR 'open' flag is set, robust to bool/int/str storage."""
    return series.map(lambda v: str(v).strip().lower() in {"1", "true"})


def run() -> None:
    """Entrypoint: load tables, derive due dates on open AR, write result."""
    logging.basicConfig(level=LOG_LEVEL, format=LOG_FORMAT)
    customers = load_table("bc_customers")
    terms = load_table("bc_payment_terms")
    ar = load_table("bc_customer_ledger_entries")
    logger.info(
        "Loaded %d customers, %d terms, %d ledger entries",
        len(customers), len(terms), len(ar),
    )

    ar_open = ar[_open_mask(ar["open"])].copy()
    logger.info("Open AR entries: %d", len(ar_open))

    lookup = build_customer_due_days(customers, terms)
    stamped = stamp_due_dates(ar_open, lookup)

    matched = stamped["customerNumber"].isin(lookup).sum()
    unmatched = len(stamped) - matched
    if unmatched:
        logger.warning(
            "%d open AR entries had no customer-master match; defaulted to %d days",
            unmatched, DEFAULT_DUE_DAYS,
        )

    n = write_to_sqlite(stamped)
    logger.info("Wrote %d rows to SQLite table %s", n, OUTPUT_TABLE)


if __name__ == "__main__":
    run()