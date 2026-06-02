# Cashflow Build — Architecture & Conventions

## What this repo is
A 13-week direct cash flow forecast for a mid-market food manufacturer. Pulls
data from Microsoft Dynamics 365 Business Central via Power Automate flows that
drop CSVs to OneDrive (plus refresh-on-open Power Query workbooks for sales- and
purchase-order data), processes them through a Python pipeline into SQLite, and
renders an Excel-shaped CFO deliverable.

Sole-developer portfolio repo (PUBLIC on GitHub). No company, customer, vendor,
or person names live in this repository; all identifiers in fixtures and docs
are synthetic, and the deliverable's organization name is supplied at runtime
via the gitignored `ORG_NAME` env var. Real data (CSVs, the SQLite db, the
rendered workbook, and the `inputs/` model assumptions) is gitignored.

## Pipeline shape

  Power Automate / Power Query  ->  OneDrive CSVs & xlsx  ->  src/extract/*.py  ->  SQLite
                                                                          |
                                                                          v
                          src/transform/*.py  -- joins, dueDate stamping --
                                                                          |
                                                                          v
                          src/calc/*.py  -- DSO/DPO, expected_*_date, bucketing,
                                            payroll, debt, revolver
                                                                          |
                                                                          v
                          src/output/excel_writer.py  -- CFO 13-week workbook

## Side mirror
AR (receivables, cash IN) and AP (payables, cash OUT) are designed as mirrors:

  AR side                              AP side
  -------                              -------
  bc_customer_ledger_entries           bc_vendor_ledger_entries        (open snapshot)
  bc_ar_history                        bc_ap_history                    (12-mo all-types)
  bc_customers                         bc_vendors                       (master)
  bc_payment_terms                     bc_payment_terms                 (SHARED)
  src/transform/due_dates.py           src/transform/ap_due_dates.py
  src/calc/dso.py                      src/calc/dpo.py
  src/calc/receipts_timing.py          src/calc/payments_timing.py
  src/calc/so_receipts_timing.py       src/calc/po_payments_timing.py  (open SO / open PO)
  src/calc/bucketing.py (AR + SO)      src/calc/bucketing.py (AP + PO)

## Sign conventions
AR ledger:
  Invoice    amount > 0    (customer owes us)
  Payment    amount < 0    (customer paid; closed unless unapplied)
  Credit Memo amount < 0   (we credited customer)

AP ledger (INVERTED):
  Invoice    amount < 0    (we owe vendor)
  Payment    amount > 0    (we paid; closed unless unapplied)
  Credit Memo amount > 0   (vendor credited us)

The dpo.py calc flips signs (`-sum()`) to express net purchases as positive.
Disbursements and receipts are surfaced as POSITIVE so bucketing aggregates
work the same for AR and AP.

## DSO/DPO fallback chain (mirrored both sides)
- balance <= 0 / NaN              -> days=0,         method="no_balance"
- trailing_net <= 0               -> days=terms_days, method="terms_fallback"
- otherwise                       -> days = balance/trailing*365, method="ratio"

Open-SO / open-PO timing applies a 3-tier collection/payment-lag waterfall off
the same per-entity table: ratio -> empirical days; no_balance/terms_fallback
-> card terms_days; entity absent -> DEFAULT_DUE_DAYS.

## Module conventions
- Module shell: load_table(), write_to_sqlite(), run() entrypoint, `if __name__=="__main__": run()`
- run() stays pure; pipeline side effects (snapshot, variance) live in __main__
- All logging via src.config (LOG_LEVEL, LOG_FORMAT)
- All SQLite via src.db.get_connection()
- Two-sided trailing window bound on history-based calcs (as_of - 365 <= postingDate <= as_of)
- Test fixtures: synthetic data + synthetic names, pytest tmp_path fixture; dates pinned where they matter
- Manually-maintained model inputs (payroll, debt schedule, revolver config) live
  in gitignored inputs/*.json; the Excel revolver math is visible formulas, not
  Python-baked values
- Live runs validate against real OneDrive data after tests pass

## Defensive bounds we have learned to add
1. Two-sided window on trailing_net_purchases / trailing_net_sales
   (one-sided PA filter is insufficient; future-dated rows would pollute basis)
2. documentType filter on the "stamping" calcs (receipts_timing, payments_timing)
   Only Invoice rows represent future cash; Payments/Credit Memos/Refunds are
   either already-cash or reductions, not future flows
3. Overdue clamp: expected_*_date = max(postingDate + days, as_of)
   tagged with was_overdue and days_overdue for downstream variance reporting

## Working-capital shape (illustrative; figures vary each refresh)
- AR open spans ~tens of $M; DSO median in the low-40-days range, collection
  peaking mid-horizon with a cliff once the open-invoice book runs out (the
  reason the open-SO layer exists -- it fills the back half).
- AP open is materially smaller than AR; DPO median in the low-teens of days.
- Customer and vendor concentration is high: a small number of accounts drive a
  large share of each side (the rationale for the queued invoice-level v2 on the
  top concentration accounts).

## Pre-scheduled payments insight
bc_ap_history can contain future-dated Payment rows (the next payment run, loaded
into BC and pre-applied to invoices, so they're closed in the open snapshot and
NOT double-counted). These are Stream A of payments_timing.py. Reversing
intercompany/lease book pairs (Invoice + Credit Memo that net to zero) are
filtered out by the documentType filter.
