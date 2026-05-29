# Cashflow Build — Architecture & Conventions

## What this repo is
13-week direct cash flow forecast for Sample Foods Co. ($900M food manufacturing, 3 plants:
Plant 1 / Plant 2 / Plant 3). Pulls data from Microsoft Dynamics 365 Business
Central via Power Automate flows that drop CSVs to OneDrive, processes them through a
Python pipeline into SQLite, and (eventually) renders an Excel-shaped CFO deliverable.

Owner: the finance manager, Finance Manager. Sole developer; portfolio repo (PUBLIC on GitHub).

## Pipeline shape

  Power Automate flows  ->  OneDrive CSVs  ->  src/extract/*.py  ->  SQLite tables
                                                                          |
                                                                          v
                          src/transform/*.py  -- joins, dueDate stamping --
                                                                          |
                                                                          v
                          src/calc/*.py  -- DSO/DPO, expected_*_date, bucketing
                                                                          |
                                                                          v
                          (next) Excel writer  -- CFO 13-week grid

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
  src/calc/receipts_timing.py          src/calc/payments_timing.py     (NEXT)
  src/calc/bucketing.py (current AR)   src/calc/bucketing.py (to extend for AP)

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
For payments_timing.py output: disbursements should be POSITIVE numbers so
bucketing aggregates work the same for AR and AP.

## DPO calc fallback chain (mirror of DSO)
- balance <= 0 / NaN              -> dpo_days=0,   method="no_balance"
- trailing_net_purchases <= 0     -> dpo_days=terms_days, method="terms_fallback"
- otherwise                       -> dpo_days = balance/purchases*365, method="ratio"

## Module conventions
- Module shell: load_table(), write_to_sqlite(), run() entrypoint, `if __name__=="__main__": run()`
- All logging via src.config (LOG_LEVEL, LOG_FORMAT)
- All SQLite via src.db.get_connection()
- Two-sided trailing window bound on history-based calcs (as_of - 365 <= postingDate <= as_of)
- Test fixtures: synthetic data, pytest tmp_path fixture; in-window dates pinned where date matters
- Live runs validate against real OneDrive data after tests pass

## Defensive bounds we have learned to add
1. Two-sided window on trailing_net_purchases / trailing_net_sales
   (one-sided PA filter is insufficient; future-dated rows would pollute basis)
2. documentType filter on the "stamping" calcs (receipts_timing, payments_timing)
   Only Invoice rows represent future cash; Payments/Credit Memos/Refunds are
   either already-cash or reductions, not future flows
3. Overdue clamp: expected_*_date = max(postingDate + days, as_of)
   tagged with was_overdue and days_overdue for downstream variance reporting

## Sample Foods working capital snapshot (current)
AR open: ~$92M total, DSO median 41d, peak collection wks 3-6, cliff at wk 7
AP open: ~$24M total, DPO median 14d (~12d consolidated)
Cash conversion gap: vendors paid 3.4x faster than customers pay us = $68M tied up

Top AR concentration: customer CUST-A at 44% of AR
Top AP concentration: 1001 Vendor A + 1002 Customer A = 53% of AP

## Pre-scheduled payments insight
bc_ap_history contains 84 future-dated Payment rows totaling $9.44M with
postingDate = 2026-06-01 (next Monday payment run). AP has loaded these
into BC and pre-applied to invoices, so they're closed (open=false) in
bc_vendor_ledger_entries. They are NOT double-counted with the open AP.
These are the input to Stream A of payments_timing.py.

Plus 8 reversing book-entry pairs (Invoice + Credit Memo, not Payment) at
2028-10-28 and 2036-01-29 that net to zero. Filtered out by documentType.