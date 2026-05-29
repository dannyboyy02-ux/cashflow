"""Per-vendor Days Payable Outstanding (DPO) from trailing 12-month purchases.

Computes an empirical days-to-pay for each vendor using:
    DPO = (balance / trailing_12mo_net_purchases) * 365

The AP-side mirror of src.calc.dso.py. Two adaptations vs the AR side:

  1. Sign convention. The vendor ledger emits invoices with NEGATIVE amounts
     (we owe more) and credit memos with POSITIVE amounts (we owe less). The
     plain sum across Invoice + Credit Memo rows is therefore negative, so we
     flip the sign to express "net purchases" as a positive number in the
     same shape as the AR-side "net sales." See ap_ledger.py for the full
     sign-convention note.

  2. Two-sided trailing window. bc_ap_history may contain rows with future
     postingDate values for two distinct reasons: pre-scheduled payments
     (legitimate; consumed elsewhere) and intercompany/lease reversing book
     pairs (illegitimate basis, net to zero). To exclude both from the DPO
     denominator, the trailing window is bounded on BOTH sides:
         as_of - WINDOW_DAYS <= postingDate <= as_of
     The one-sided lower bound used on the AR side is not sufficient here.

Fallback chain per vendor:
  - balance <= 0 or NaN              -> DPO = 0, method = "no_balance"
  - trailing_net_purchases <= 0      -> DPO = terms_days, method = "terms_fallback"
  - otherwise                        -> DPO = balance / purchases * 365, method = "ratio"

The output table feeds the payments-timing layer (next module), which uses
each vendor's DPO to stamp expected_payment_date = postingDate + dpo_days
on every open AP row that isn't already covered by a pre-scheduled payment.

Strategic note: empirical DPO and contractual terms often diverge materially
in real data -- a conservative AP department capturing early-pay discounts
will produce a DPO well below the contractual mode (e.g. ~12 days observed
vs NET-30 contract). The "ratio" method surfaces this empirically; the
"terms_fallback" is only used when there's no trailing activity to measure.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Optional

import pandas as pd

from src.config import LOG_LEVEL, LOG_FORMAT
from src.db import get_connection
from src.transform.ap_due_dates import build_vendor_due_days, DEFAULT_DUE_DAYS

logger = logging.getLogger(__name__)

WINDOW_DAYS = 365

PURCHASE_DOC_TYPES = ("Invoice", "Credit Memo")

OUTPUT_TABLE = "bc_vendor_dpo"

METHOD_RATIO = "ratio"
METHOD_NO_BALANCE = "no_balance"
METHOD_TERMS_FALLBACK = "terms_fallback"


def trailing_net_purchases(
    history: pd.DataFrame,
    as_of: Optional[dt.date] = None,
    window_days: int = WINDOW_DAYS,
) -> pd.Series:
    """Per-vendor sign-flipped sum of Invoice + Credit Memo amounts in window.

    The window is bounded on BOTH sides:
        as_of - window_days <= postingDate <= as_of

    Lower bound: matches the PA filter (12-month trailing).
    Upper bound: excludes future-dated rows (pre-scheduled payments don't
        appear here anyway because Payment is not a purchase doctype, but
        future-dated reversing book pairs WOULD pollute the basis without
        this bound).

    Sign flip: invoices are negative in the AP ledger and credit memos
    positive, so plain sum is negative. -sum() yields net purchases as a
    positive number. Vendors absent from the result had no purchase activity
    in the window.
    """
    if as_of is None:
        as_of = dt.date.today()
    window_start = as_of - dt.timedelta(days=window_days)

    posting = pd.to_datetime(history["postingDate"]).dt.date
    in_window = (posting >= window_start) & (posting <= as_of)
    is_purchase = history["documentType"].isin(PURCHASE_DOC_TYPES)
    purchases = history[in_window & is_purchase]

    return -purchases.groupby("vendorNumber")["amount"].sum()


def compute_dpo(
    balance: float,
    trailing_purchases: float,
    terms_days: int,
    window_days: int = WINDOW_DAYS,
) -> tuple[int, str]:
    """Return (dpo_days, method) for one vendor. See module docstring."""
    if balance is None or pd.isna(balance) or balance <= 0:
        return 0, METHOD_NO_BALANCE
    if trailing_purchases is None or pd.isna(trailing_purchases) or trailing_purchases <= 0:
        return int(terms_days), METHOD_TERMS_FALLBACK
    dpo = int(round((balance / trailing_purchases) * window_days))
    return dpo, METHOD_RATIO


def build_vendor_dpo(
    vendors: pd.DataFrame,
    history: pd.DataFrame,
    terms: pd.DataFrame,
    as_of: Optional[dt.date] = None,
) -> pd.DataFrame:
    """Compute the per-vendor DPO table.

    Note: bc_vendors.balance is already positive (BC's master FlowField
    presents AP as a positive "what we owe" number despite the ledger entries
    carrying negative amounts). No sign flip needed on the balance read.
    """
    purchases_by_vendor = trailing_net_purchases(history, as_of=as_of)
    terms_lookup = build_vendor_due_days(vendors, terms)

    rows: list[dict] = []
    for _, vendor in vendors.iterrows():
        vn = vendor["number"]
        balance_raw = vendor["balance"]
        balance = 0.0 if pd.isna(balance_raw) else float(balance_raw)
        purchases = float(purchases_by_vendor.get(vn, 0.0))
        terms_days = int(terms_lookup.get(vn, DEFAULT_DUE_DAYS))
        dpo_days, method = compute_dpo(balance, purchases, terms_days)
        rows.append(
            {
                "vendorNumber": vn,
                "balance": balance,
                "trailing_net_purchases": purchases,
                "terms_days": terms_days,
                "dpo_days": dpo_days,
                "dpo_method": method,
            }
        )
    return pd.DataFrame(rows)


def load_table(name: str) -> pd.DataFrame:
    with get_connection() as conn:
        return pd.read_sql(f"SELECT * FROM {name}", conn)


def write_to_sqlite(df: pd.DataFrame, table_name: str = OUTPUT_TABLE) -> int:
    with get_connection() as conn:
        df.to_sql(table_name, conn, if_exists="replace", index=False)
    return len(df)


def _summary(df: pd.DataFrame) -> dict:
    method_counts = df["dpo_method"].value_counts().to_dict()
    with_balance = df[df["balance"] > 0]
    summary = {"methods": method_counts, "vendors_with_ap": int(len(with_balance))}
    if not with_balance.empty:
        summary["median_dpo"] = int(with_balance["dpo_days"].median())
        summary["max_dpo"] = int(with_balance["dpo_days"].max())
    return summary


def run() -> None:
    logging.basicConfig(level=LOG_LEVEL, format=LOG_FORMAT)
    vendors = load_table("bc_vendors")
    history = load_table("bc_ap_history")
    terms = load_table("bc_payment_terms")
    logger.info(
        "Loaded %d vendors, %d history rows, %d terms",
        len(vendors), len(history), len(terms),
    )

    dpo_df = build_vendor_dpo(vendors, history, terms)
    logger.info("DPO summary: %s", _summary(dpo_df))

    n = write_to_sqlite(dpo_df)
    logger.info("Wrote %d rows to SQLite table %s", n, OUTPUT_TABLE)


if __name__ == "__main__":
    run()