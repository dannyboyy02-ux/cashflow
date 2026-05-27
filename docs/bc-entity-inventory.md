# BC Entity Inventory — Cash Flow Workbook

Reference for the five Power Automate flows extracting from D365 Business Central into OneDrive. Compiled from Microsoft Learn documentation current as of May 2026 (BC v28, 2026 Release Wave 1).

## Scope

- Single BC company, consolidated company-level data only.
- **No global dimensions in scope.** No SITE, no department, no shortcut dimensions on any entity. The 13-week cash flow operates at the highest consolidated level.
- Standard Power Automate Business Central connector for all flows. No custom PA connector required.

## Standard PA BC connector behavior

The standard BC connector exposes both the built-in `v2.0` namespace AND any custom APIs registered in the tenant — Microsoft's `microsoft/reportsFinance/beta`, and any custom API pages we publish — via its "API category" dropdown. One connector covers all five flows; only the API category and table name change per flow.

## The five flows

### Flow 1 — AR (customer ledger entries, open + recent paid, last 24 months)

- BC source table: Cust. Ledger Entry (T21)
- Standard v2.0: not available
- Primary path: `microsoft/reportsFinance/beta/customerLedgerEntries`
- Status: Beta (live and stable in production usage since BC21 / 2022 Wave 2)
- Fallback: publish custom OData page on T21 if Microsoft changes the beta endpoint

### Flow 2 — AP (vendor ledger entries, open + recent paid, last 24 months)

- BC source table: Vendor Ledger Entry (T25)
- Standard v2.0: not available
- Primary path: `microsoft/reportsFinance/beta/vendorLedgerEntries` (confirm exists in tenant during build)
- Status: Beta — same risk profile as Flow 1
- Fallback: publish custom OData page on T25

### Flow 3 — GL entries (current and prior FY for cash, revolver, A/R, A/P accounts)

- BC source table: G/L Entry (T17)
- Standard v2.0: `generalLedgerEntries` — available
- Properties exposed: id, entryNumber, postingDate, documentNumber, documentType, accountId, accountNumber, description, debitAmount, creditAmount, additionalCurrencyDebitAmount, additionalCurrencyCreditAmount, lastModifiedDateTime
- Sufficient for consolidated-company cash flow needs
- No fallback path needed

### Flow 4 — Customer and Vendor master

- BC source tables: Customer (T18), Vendor (T23), Payment Terms (T3), Currency (T4)
- Standard v2.0: all available
- Primary path: `v2.0/customers`, `v2.0/vendors` with `$expand=paymentTerm, currency, customerFinancialDetails`
- No friction expected

### Flow 5 — Open SOs and POs

- BC source tables: Sales Header (T36), Purchase Header (T38), filtered to status=Open
- Standard v2.0: `salesOrders` and `purchaseOrders` available with `$filter=status eq 'Open'`
- No friction expected

## Risks

- **Beta endpoint risk (Flows 1 and 2):** `reportsFinance/beta` has been stable in production usage since 2022 Wave 2 but remains officially Beta. Mitigation: documented custom OData page recipe for T21 / T25 so we can pivot in under a day if Microsoft makes a breaking change.
- **API v1.0 removed in BC v28 (2026 Release Wave 1, April 2026):** all flows must target v2.0 stable or custom APIs. No v1.0 references anywhere.

## What to verify in the BC tenant before the Flow 1 build

1. In Power Automate, in a new BC trigger or action, confirm `microsoft/reportsFinance/beta` appears in the API category dropdown.
2. Within that API category, confirm `customerLedgerEntries` and `vendorLedgerEntries` appear in the Table name dropdown.
3. Note the field list returned by a sample query — at minimum: customer number, posting date, due date, document type, document number, original amount, remaining amount, open boolean, applied/closed status.