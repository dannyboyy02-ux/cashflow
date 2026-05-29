"""Stamp expected_payment_date on each future AP disbursement (two streams).

The AP-side mirror of src.calc.receipts_timing.py, with one structural addition:
AP disbursements arrive from TWO sources, not one, so the output is the concat
of two independently-built streams.

STREAM A -- Deterministic future payments (from bc_ap_history)

AP enters the next payment run into BC ahead of time: each Payment row is given
a future postingDate (the disbursement date) and is pre-applied to its target
invoices. Because it's pre-applied, the payment is CLOSED (open=false) in
bc_vendor_ledger_entries, so it never appears in the open-AP snapshot that
Stream B reads -- the two streams cannot double-count the same cash.

These payments need no estimation. We read documentType=='Payment' rows with
postingDate > as_of straight from the 12-month history table and stamp:
    expected_payment_date = postingDate   (verbatim; it IS the scheduled date)
    timing_method         = "scheduled"
    was_overdue           = False          (scheduled in the future by definition)
    source_stream         = "scheduled"

The future-dated reversing book pairs (Invoice + Credit Memo at 2028/2036 that
net to zero) are NOT Payment rows, so the documentType filter drops them with
no extra logic.

STREAM B -- Estimated payments from open AP (mirror of receipts_timing.py)

For each open AP Invoice entry:
    raw_payment_date      = postingDate + vendor's dpo_days
    expected_payment_date = max(raw_payment_date, as_of)   (overdue clamp)

DOCUMENT-TYPE FILTER (mirror of the receipts side): only Invoice rows are
future disbursements. Open Payments (unapplied cash already out), open Credit
Memos (reductions to what we owe), and Refunds are economic events that don't
represent future outflows and must not be timed into future weeks.

The dpo_method from bc_vendor_dpo flows through as timing_method. A vendor that
appears on an open AP row but is missing from bc_vendor_dpo (data-quality miss)
falls back to the row's dueDays with timing_method = "master_fallback". Note we
do NOT fall back when DPO returns method "no_balance" (dpo_days=0): that's a
legitimate ratio-chain outcome meaning "pay immediately", which the overdue
clamp then lifts to as_of. We only fall back when the vendor is wholly absent
from the DPO table -- exactly mirroring receipts_timing.py.

OUTPUT (both streams concatenated): ap_disbursements_with_expected_payment_date.

disbursement_amount is POSITIVE on both streams so the bucketing layer can
aggregate AR and AP identically:
    Stream A: disbursement_amount =  amount   (AP Payment rows are positive)
    Stream B: disbursement_amount = -amount   (AP Invoice rows are negative; flip)
The raw signed `amount` is preserved on every row for traceability.

Column note: Stream B carries dueDays/dueDate from the ap_due_dates transform;
Stream A (sourced from bc_ap_history) does not. The concat leaves NaN in those
columns for Stream A rows -- expected and harmless.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Optional

import pandas as pd

from src.config import LOG_LEVEL, LOG_FORMAT
from src.db import get_connection

logger = logging.getLogger(__name__)

OUTPUT_TABLE = "ap_disbursements_with_expected_payment_date"

# Stream B: documentTypes eligible for the disbursements forecast. Only Invoice
# qualifies -- see module docstring for the rationale on excluding Payment,
# Credit Memo, and Refund (mirror of receipts_timing.RECEIPT_DOC_TYPES).
DISBURSEMENT_DOC_TYPES = ("Invoice",)

# Stream A: the single documentType that carries a pre-scheduled future payment.
SCHEDULED_DOC_TYPE = "Payment"

# Stream B method label applied when a vendor appears on an open AP row but
# isn't present in bc_vendor_dpo (data-quality miss; the master should cover
# every vendor with open AP). In that case we fall back to the row's dueDays.
METHOD_MASTER_FALLBACK = "master_fallback"

# Stream A method / stream labels.
METHOD_SCHEDULED = "scheduled"
STREAM_SCHEDULED = "scheduled"
STREAM_ESTIMATED = "estimated"


def build_scheduled_payments(
    history: pd.DataFrame,
    as_of: Optional[dt.date] = None,
) -> pd.DataFrame:
    """Stream A: pre-scheduled future payments read verbatim from bc_ap_history.

    Selects documentType=='Payment' rows with postingDate strictly after as_of
    (past/equal-dated payments are already cash out and belong to history, not
    the forecast). Each surviving row is stamped with expected_payment_date ==
    postingDate -- no estimation, since AP has already committed the date.

    Returns the raw history columns plus the standard timing columns. When no
    future-dated payments exist, returns the same column shape with zero rows
    so the downstream concat stays clean.
    """
    if as_of is None:
        as_of = dt.date.today()

    posting = pd.to_datetime(history["postingDate"]).dt.date
    is_payment = history["documentType"] == SCHEDULED_DOC_TYPE
    is_future = posting > as_of
    df = history[is_payment & is_future].copy()

    logger.info(
        "Stream A: %d future-dated Payment rows (postingDate > %s) of %d history rows",
        len(df), as_of.isoformat(), len(history),
    )

    # Normalize postingDate to dt.date and stamp the scheduled date verbatim.
    df["postingDate"] = pd.to_datetime(df["postingDate"]).dt.date
    df["expected_payment_date"] = df["postingDate"]
    df["dpo_days_effective"] = 0
    df["timing_method"] = METHOD_SCHEDULED
    df["was_overdue"] = False
    df["days_overdue"] = 0
    # AP Payment amounts are already positive; disbursement is the amount as-is.
    df["disbursement_amount"] = df["amount"].astype(float)
    df["source_stream"] = STREAM_SCHEDULED

    return df


def stamp_expected_payment_dates(
    ap: pd.DataFrame,
    dpo_df: pd.DataFrame,
    as_of_date: dt.date,
) -> pd.DataFrame:
    """Stream B: stamp expected_payment_date on open AP Invoice rows from DPO.

    Filters input to DISBURSEMENT_DOC_TYPES first (Invoice only) -- see module
    docstring. Then joins the per-vendor DPO (dpo_days + dpo_method) onto each
    AP row and computes raw_payment_date = postingDate + dpo_days. Overdue rows
    (raw_payment_date < as_of_date) are clamped to as_of_date and tagged.

    Vendors in open AP but missing from dpo_df (rare) fall back to the row's
    dueDays, with timing_method = "master_fallback". The clamp logic still
    applies; only the source of the day count differs.
    """
    # Disbursement-eligible filter: only Invoice rows represent future cash out.
    # Payment / Credit Memo / Refund rows on the open AP are economic events
    # that don't add to the forecast and must not be timed into future weeks.
    n_before = len(ap)
    ap = ap[ap["documentType"].isin(DISBURSEMENT_DOC_TYPES)].copy()
    n_filtered = n_before - len(ap)
    if n_filtered > 0:
        logger.info(
            "Stream B: filtered %d non-disbursement rows "
            "(Payment/Credit Memo/Refund/blank) from %d open AP; "
            "%d Invoice rows remain for stamping",
            n_filtered, n_before, len(ap),
        )

    # Left-join in DPO data. Missing matches produce NaN in dpo_days/dpo_method.
    df = ap.merge(
        dpo_df[["vendorNumber", "dpo_days", "dpo_method"]],
        on="vendorNumber",
        how="left",
    )

    # Effective DPO: empirical (or terms-based) DPO from the lookup if present,
    # otherwise fall back to the row's dueDays (already terms-based from the
    # ap-due-dates transform). We fall back ONLY when the vendor is absent from
    # the DPO table -- a "no_balance" dpo_days=0 is a legitimate value that the
    # overdue clamp will lift to as_of, not a reason to fall back.
    df["dpo_days_effective"] = df["dpo_days"].fillna(df["dueDays"]).astype(int)
    df["timing_method"] = df["dpo_method"].fillna(METHOD_MASTER_FALLBACK)
    df = df.drop(columns=["dpo_days", "dpo_method"])

    # Date arithmetic in pandas Timestamp space, then convert back to dt.date
    # for SQLite storage (matches the receipts_timing / stamp_due_dates pattern).
    posting = pd.to_datetime(df["postingDate"])
    raw_payment = posting + pd.to_timedelta(df["dpo_days_effective"], unit="D")
    as_of_ts = pd.Timestamp(as_of_date)

    df["was_overdue"] = raw_payment < as_of_ts
    df["days_overdue"] = (as_of_ts - raw_payment).dt.days.clip(lower=0).astype(int)
    df["expected_payment_date"] = raw_payment.clip(lower=as_of_ts).dt.date
    df["postingDate"] = posting.dt.date
    # AP Invoice amounts are negative; flip to a positive disbursement.
    df["disbursement_amount"] = -df["amount"].astype(float)
    df["source_stream"] = STREAM_ESTIMATED

    return df


def build_disbursements(
    ap: pd.DataFrame,
    dpo_df: pd.DataFrame,
    history: pd.DataFrame,
    as_of_date: dt.date,
) -> pd.DataFrame:
    """Combine Stream A (scheduled) and Stream B (estimated) into one table.

    The two streams carry slightly different column sets (Stream B has the
    dueDays/dueDate transform columns, Stream A does not); pd.concat unions the
    columns and fills the gaps with NaN. Both streams share the standard timing
    columns (expected_payment_date, timing_method, was_overdue, days_overdue,
    disbursement_amount, source_stream) so downstream bucketing is uniform.
    """
    scheduled = build_scheduled_payments(history, as_of=as_of_date)
    estimated = stamp_expected_payment_dates(ap, dpo_df, as_of_date)
    combined = pd.concat([scheduled, estimated], ignore_index=True)
    return combined


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
    """Diagnostic summary for the log line.

    Splits scheduled vs estimated dollars (the two streams) and, within the
    estimated stream, the overdue clamp volume -- the one place the model
    exercises judgment. Worth seeing every run.
    """
    scheduled = df[df["source_stream"] == STREAM_SCHEDULED]
    estimated = df[df["source_stream"] == STREAM_ESTIMATED]
    overdue = estimated[estimated["was_overdue"]]
    future = estimated[~estimated["was_overdue"]]
    return {
        "rows": int(len(df)),
        "as_of": as_of.isoformat(),
        "scheduled_rows": int(len(scheduled)),
        "scheduled_dollars": round(float(scheduled["disbursement_amount"].sum()), 2),
        "estimated_rows": int(len(estimated)),
        "estimated_dollars": round(float(estimated["disbursement_amount"].sum()), 2),
        "estimated_overdue_rows": int(len(overdue)),
        "estimated_overdue_dollars": round(float(overdue["disbursement_amount"].sum()), 2),
        "estimated_future_rows": int(len(future)),
        "estimated_future_dollars": round(float(future["disbursement_amount"].sum()), 2),
        "timing_methods": df["timing_method"].value_counts().to_dict(),
    }


def run(as_of_date: Optional[dt.date] = None) -> None:
    """Entrypoint: load tables, build both streams, write the combined result."""
    logging.basicConfig(level=LOG_LEVEL, format=LOG_FORMAT)
    if as_of_date is None:
        as_of_date = dt.date.today()
    ap = load_table("ap_open_with_due_dates")
    dpo_df = load_table("bc_vendor_dpo")
    history = load_table("bc_ap_history")
    logger.info(
        "Loaded %d open AP rows, %d DPO rows, %d history rows; as_of=%s",
        len(ap), len(dpo_df), len(history), as_of_date.isoformat(),
    )

    combined = build_disbursements(ap, dpo_df, history, as_of_date)
    logger.info("Disbursements summary: %s", _summary(combined, as_of_date))

    n = write_to_sqlite(combined)
    logger.info("Wrote %d rows to SQLite table %s", n, OUTPUT_TABLE)


if __name__ == "__main__":
    run()
