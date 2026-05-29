"""Per-customer Days Sales Outstanding (DSO) from trailing 12-month sales.

Computes an empirical days-to-collect for each customer using:
    DSO = (balance_due / trailing_12mo_net_sales) * 365

Net sales nets Invoice + Credit Memo amounts (signed -- the BC ledger emits
invoices positive and credit memos negative). Payments and refunds are
excluded; they're cash movements, not sales.

Method note: the textbook "countback" DSO walks back month-by-month subtracting
sales from AR. That approach is well-behaved at the aggregate company level
but breaks down at per-customer granularity because customer-level monthly
sales are lumpy and frequently zero. The simple trailing ratio used here gives
the same fundamental answer (days of AR relative to recent sales velocity) and
is mathematically robust on noisy per-customer data, at the cost of countback's
recency-weighting. For a stable mid-market manufacturer on a 13-week forecast
horizon, that tradeoff is acceptable.

Fallback chain per customer:
  - balance_due <= 0 or NaN          -> DSO = 0, method = "no_balance"
  - trailing_net_sales <= 0           -> DSO = terms_days, method = "terms_fallback"
  - otherwise                         -> DSO = balance / sales * 365, method = "ratio"

The output table feeds the receipts-timing layer (next module), which uses each
customer's DSO to stamp expected_collection_date = postingDate + dso_days on
every open AR row.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Optional

import pandas as pd

from src.config import LOG_LEVEL, LOG_FORMAT
from src.db import get_connection
from src.transform.due_dates import build_customer_due_days, DEFAULT_DUE_DAYS

logger = logging.getLogger(__name__)

# Window over which trailing sales are measured. Bounded on BOTH sides
# (as_of - WINDOW_DAYS <= postingDate <= as_of) so that:
#   - stale history pulls (if the PA filter were ever widened beyond 12mo)
#     don't silently understate DSO by adding pre-window sales to the basis;
#   - future-dated rows (rare on the AR side but architecturally possible --
#     book entries, intercompany pairs) don't pollute the basis.
# The one-sided upstream PA filter alone is not sufficient.
WINDOW_DAYS = 365

# documentTypes that count as credit sales for DSO. Payment and Refund are
# cash movements; blank-documentType rows are rare manual journal entries.
SALES_DOC_TYPES = ("Invoice", "Credit Memo")

OUTPUT_TABLE = "bc_customer_dso"

# Methods recorded on each output row so downstream consumers can distinguish
# empirically-derived DSOs from fallback values.
METHOD_RATIO = "ratio"
METHOD_NO_BALANCE = "no_balance"
METHOD_TERMS_FALLBACK = "terms_fallback"


def trailing_net_sales(
    history: pd.DataFrame,
    as_of: Optional[dt.date] = None,
    window_days: int = WINDOW_DAYS,
) -> pd.Series:
    """Per-customer signed sum of Invoice + Credit Memo amounts in window.

    The window is bounded on BOTH sides:
        as_of - window_days <= postingDate <= as_of

    Lower bound: matches the upstream PA filter (12-month trailing).
    Upper bound: defends against future-dated rows polluting the sales basis.

    Invoice amounts are positive, credit memos negative in BC's customer
    ledger, so plain sum nets correctly. Customers absent from the result
    have no sales activity in the window.
    """
    if as_of is None:
        as_of = dt.date.today()
    window_start = as_of - dt.timedelta(days=window_days)

    posting = pd.to_datetime(history["postingDate"]).dt.date
    in_window = (posting >= window_start) & (posting <= as_of)
    is_sale = history["documentType"].isin(SALES_DOC_TYPES)
    sales = history[in_window & is_sale]

    return sales.groupby("customerNumber")["amount"].sum()


def compute_dso(
    balance: float,
    trailing_sales: float,
    terms_days: int,
    window_days: int = WINDOW_DAYS,
) -> tuple[int, str]:
    """Return (dso_days, method) for one customer.

    See module docstring for the fallback chain. dso_days is returned as a
    non-negative int (rounded to the nearest day). For ratio cases the returned
    value can exceed window_days when balance_due exceeds trailing sales -- a
    real signal that the customer carries materially aged AR, surfaced rather
    than capped so downstream layers can decide how to treat it.
    """
    # pd.isna handles None, pd.NA, NaN uniformly
    if balance is None or pd.isna(balance) or balance <= 0:
        return 0, METHOD_NO_BALANCE
    if trailing_sales is None or pd.isna(trailing_sales) or trailing_sales <= 0:
        return int(terms_days), METHOD_TERMS_FALLBACK
    dso = int(round((balance / trailing_sales) * window_days))
    return dso, METHOD_RATIO


def build_customer_dso(
    customers: pd.DataFrame,
    history: pd.DataFrame,
    terms: pd.DataFrame,
    as_of: Optional[dt.date] = None,
) -> pd.DataFrame:
    """Compute the per-customer DSO table.

    Iterates every customer in the master, joining trailing sales (left join,
    missing means zero) and the terms-based dueDays from the existing transform
    layer. Returns a DataFrame ready for SQLite.
    """
    sales_by_cust = trailing_net_sales(history, as_of=as_of)
    terms_lookup = build_customer_due_days(customers, terms)

    rows: list[dict] = []
    for _, cust in customers.iterrows():
        cn = cust["number"]
        balance_raw = cust["balanceDue"]
        balance = 0.0 if pd.isna(balance_raw) else float(balance_raw)
        sales = float(sales_by_cust.get(cn, 0.0))
        terms_days = int(terms_lookup.get(cn, DEFAULT_DUE_DAYS))
        dso_days, method = compute_dso(balance, sales, terms_days)
        rows.append(
            {
                "customerNumber": cn,
                "balance_due": balance,
                "trailing_net_sales": sales,
                "terms_days": terms_days,
                "dso_days": dso_days,
                "dso_method": method,
            }
        )
    return pd.DataFrame(rows)


def load_table(name: str) -> pd.DataFrame:
    """Read an entire SQLite table into a DataFrame."""
    with get_connection() as conn:
        return pd.read_sql(f"SELECT * FROM {name}", conn)


def write_to_sqlite(df: pd.DataFrame, table_name: str = OUTPUT_TABLE) -> int:
    """Replace the table contents with df. Returns the number of rows written."""
    with get_connection() as conn:
        df.to_sql(table_name, conn, if_exists="replace", index=False)
    return len(df)


def _summary(df: pd.DataFrame) -> dict:
    """Diagnostic summary for the log line."""
    method_counts = df["dso_method"].value_counts().to_dict()
    with_balance = df[df["balance_due"] > 0]
    summary = {"methods": method_counts, "customers_with_ar": int(len(with_balance))}
    if not with_balance.empty:
        summary["median_dso"] = int(with_balance["dso_days"].median())
        summary["max_dso"] = int(with_balance["dso_days"].max())
    return summary


def run() -> None:
    """Entrypoint: load tables, compute DSOs, write result."""
    logging.basicConfig(level=LOG_LEVEL, format=LOG_FORMAT)
    customers = load_table("bc_customers")
    history = load_table("bc_ar_history")
    terms = load_table("bc_payment_terms")
    logger.info(
        "Loaded %d customers, %d history rows, %d terms",
        len(customers), len(history), len(terms),
    )

    dso_df = build_customer_dso(customers, history, terms)
    logger.info("DSO summary: %s", _summary(dso_df))

    n = write_to_sqlite(dso_df)
    logger.info("Wrote %d rows to SQLite table %s", n, OUTPUT_TABLE)


if __name__ == "__main__":
    run()
