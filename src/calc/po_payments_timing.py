"""Time open-PO liabilities into future AP disbursements (two sub-streams).

The PO-side analogue of src.calc.so_receipts_timing on the AP side. Each open PO
line carries up to two distinct future-cash claims, emitted as separate rows:

  RBNI (received but not invoiced) -- goods physically in hand, no AP entry yet.
      Becomes a vendor invoice imminently (INVOICE_LAG_DAYS from now), then is
      paid after the vendor's payment lag:
          expected_payment_date = as_of + INVOICE_LAG_DAYS + lag_days
      Not overdue by construction (the invoice hasn't even posted yet).

  PO outstanding (ordered, not yet received) -- receives on the line's
      expectedReceiptDate, is invoiced shortly after, then paid:
          raw_expected_invoice = expectedReceiptDate + INVOICE_LAG_DAYS
          raw_expected_payment = raw_expected_invoice + lag_days
          expected_payment_date = max(raw_expected_payment, as_of)   (overdue clamp)

PAYMENT-LAG WATERFALL (3 tiers, resolved per vendor from bc_vendor_dpo, the same
shape as the SO collection-lag waterfall):
  Tier 1  dpo_method == 'ratio'                      -> lag = dpo_days
  Tier 2  dpo_method in ('no_balance','terms_fallback') -> lag = terms_days
  Tier 3  vendor absent from bc_vendor_dpo            -> lag = DEFAULT_DUE_DAYS

A partial-receipt line (rbniAmount > 0 AND outstandingAmount > 0) emits BOTH
rows. Zero-amount sub-streams emit nothing.

Output table: po_open_with_expected_payment -- both sub-streams co-mingled,
distinguished by source_stream ('po_rbni' | 'po_outstanding'). Bucketing keys on
source_stream so each week keeps the two streams separate.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path
from typing import Optional

import pandas as pd

from src.config import LOG_LEVEL, LOG_FORMAT, INPUTS_DIR
from src.db import get_connection
from src.calc.dpo import METHOD_RATIO
from src.transform.ap_due_dates import DEFAULT_DUE_DAYS

logger = logging.getLogger(__name__)

INPUT_TABLE = "po_open_lines"
OUTPUT_TABLE = "po_open_with_expected_payment"

# Optional model-assumption config (gitignored). Holds the FP&A conversion
# haircut on Tier-3 PO outstanding (ordered, not yet received). Default 0.0 =
# the conservative gross view (no change to prior behavior).
PO_CONFIG_FILE = INPUTS_DIR / "po_config.json"

INVOICE_LAG_DAYS = 7    # days from goods-received to vendor-invoice-posted

SOURCE_STREAM_RBNI = "po_rbni"
SOURCE_STREAM_OUTSTANDING = "po_outstanding"

METHOD_DEFAULT = "default"   # payment_method tag for Tier 3 (vendor absent)

# Columns carried from each PO line onto every emitted disbursement row.
_BASE_COLUMNS = [
    "poNumber", "lineNumber", "vendorNumber", "vendorName",
    "itemNumber", "description", "Type",
]
OUTPUT_COLUMNS = _BASE_COLUMNS + [
    "amount", "source_stream", "expected_payment_date",
    "payment_method", "was_overdue", "days_overdue",
]


def load_outstanding_haircut(path: Optional[Path] = None) -> float:
    """Read the Tier-3 PO-outstanding conversion haircut (fraction), default 0.0.

    A haircut of 0.30 means only 70% of each open PO line's outstanding amount
    is timed as a future disbursement, reflecting that ordered-not-received POs
    can still be changed/cancelled before receipt (FP&A lower-certainty tier).
    """
    path = Path(path) if path is not None else PO_CONFIG_FILE
    if not path.exists():
        return 0.0
    with open(path) as f:
        return float(json.load(f).get("outstanding_haircut_pct", 0.0))


def _resolve_lag(df: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """Return (lag_days int Series, payment_method str Series) via the 3-tier waterfall."""
    present = df["dpo_method"].notna()
    is_ratio = present & (df["dpo_method"] == METHOD_RATIO)   # Tier 1
    is_terms = present & ~is_ratio                            # Tier 2

    dpo_days = pd.to_numeric(df["dpo_days"], errors="coerce")
    terms_days = pd.to_numeric(df["terms_days"], errors="coerce")

    lag = pd.Series(float(DEFAULT_DUE_DAYS), index=df.index)
    lag = lag.mask(is_terms, terms_days)
    lag = lag.mask(is_ratio, dpo_days)
    lag = lag.fillna(DEFAULT_DUE_DAYS).astype(int)

    method = df["dpo_method"].fillna(METHOD_DEFAULT)
    return lag, method


def build_po_payments(
    po_lines: pd.DataFrame,
    dpo_df: pd.DataFrame,
    as_of: dt.date,
    outstanding_haircut: float = 0.0,
) -> pd.DataFrame:
    """Emit up to two disbursement rows per open PO line (RBNI + outstanding).

    Joins bc_vendor_dpo for the payment-lag waterfall, then builds the two
    sub-streams and concatenates. The Tier-3 outstanding stream is scaled by
    (1 - outstanding_haircut) -- RBNI (Tier 2, goods in hand) is never haircut.
    Returns the OUTPUT_COLUMNS shape.
    """
    df = po_lines.merge(
        dpo_df[["vendorNumber", "dpo_days", "dpo_method", "terms_days"]],
        on="vendorNumber",
        how="left",
    )
    df["lag_days"], df["payment_method"] = _resolve_lag(df)
    as_of_ts = pd.Timestamp(as_of)

    # RBNI sub-stream: invoiced from now, paid after the lag. Never overdue.
    rbni_src = df[df["rbniAmount"] > 0].copy()
    rbni = rbni_src[_BASE_COLUMNS].copy()
    rbni["amount"] = rbni_src["rbniAmount"].astype(float)
    rbni["source_stream"] = SOURCE_STREAM_RBNI
    rbni["expected_payment_date"] = (
        as_of_ts + pd.to_timedelta(INVOICE_LAG_DAYS + rbni_src["lag_days"], unit="D")
    ).dt.date
    rbni["payment_method"] = rbni_src["payment_method"]
    rbni["was_overdue"] = False
    rbni["days_overdue"] = 0

    # Outstanding sub-stream: received on expectedReceiptDate, then invoiced+paid.
    out_src = df[df["outstandingAmount"] > 0].copy()
    out = out_src[_BASE_COLUMNS].copy()
    # Tier-3 conversion haircut: only (1 - haircut) of the ordered-not-received
    # amount is timed as future cash.
    out["amount"] = (out_src["outstandingAmount"].astype(float) * (1.0 - outstanding_haircut)).round(2)
    out["source_stream"] = SOURCE_STREAM_OUTSTANDING
    # A missing expectedReceiptDate (blank in BC) is treated as received as-of
    # (imminent): keeps the line in the forecast rather than dropping it, and
    # avoids NaT propagating into the integer day-count columns.
    recv = pd.to_datetime(out_src["expectedReceiptDate"]).fillna(as_of_ts)
    raw_payment = recv + pd.to_timedelta(INVOICE_LAG_DAYS + out_src["lag_days"], unit="D")
    out["expected_payment_date"] = raw_payment.clip(lower=as_of_ts).dt.date
    out["payment_method"] = out_src["payment_method"]
    out["was_overdue"] = raw_payment < as_of_ts
    out["days_overdue"] = (as_of_ts - raw_payment).dt.days.clip(lower=0).astype(int)

    result = pd.concat([rbni, out], ignore_index=True)
    if result.empty:
        return pd.DataFrame(columns=OUTPUT_COLUMNS)
    return result[OUTPUT_COLUMNS]


def load_table(name: str) -> pd.DataFrame:
    """Read an entire SQLite table into a DataFrame."""
    with get_connection() as conn:
        return pd.read_sql(f"SELECT * FROM {name}", conn)


def write_to_sqlite(df: pd.DataFrame, table_name: str = OUTPUT_TABLE) -> int:
    """Replace the table contents with df. Returns the number of rows written."""
    with get_connection() as conn:
        df.to_sql(table_name, conn, if_exists="replace", index=False)
    return len(df)


def _summary(df: pd.DataFrame, as_of: dt.date) -> dict:
    by_stream = df.groupby("source_stream")["amount"].agg(["count", "sum"]) if not df.empty else None
    return {
        "rows": int(len(df)),
        "as_of": as_of.isoformat(),
        "invoice_lag_days": INVOICE_LAG_DAYS,
        "by_source_stream": {
            s: {"rows": int(r["count"]), "amount": round(float(r["sum"]), 2)}
            for s, r in (by_stream.iterrows() if by_stream is not None else [])
        },
        "payment_methods": df["payment_method"].value_counts().to_dict() if not df.empty else {},
    }


def run(as_of: Optional[dt.date] = None) -> None:
    """Entrypoint: load open PO lines + DPO, build timed disbursements, write."""
    logging.basicConfig(level=LOG_LEVEL, format=LOG_FORMAT)
    if as_of is None:
        as_of = dt.date.today()
    po_lines = load_table(INPUT_TABLE)
    dpo_df = load_table("bc_vendor_dpo")
    logger.info(
        "Loaded %d open PO lines, %d DPO rows; as_of=%s",
        len(po_lines), len(dpo_df), as_of.isoformat(),
    )

    haircut = load_outstanding_haircut()
    result = build_po_payments(po_lines, dpo_df, as_of, outstanding_haircut=haircut)
    logger.info("PO payments timing summary (Tier-3 haircut=%.2f): %s", haircut, _summary(result, as_of))

    n = write_to_sqlite(result)
    logger.info("Wrote %d rows to SQLite table %s", n, OUTPUT_TABLE)


if __name__ == "__main__":
    run()
