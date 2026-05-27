# BC Entity Inventory — Cash Flow Workbook

Reference for the five Power Automate flows extracting from D365 Business Central into OneDrive. Compiled from Microsoft Learn documentation current as of May 2026 (BC v28, 2026 Release Wave 1), refined after Flow 1 verification and tenant permission constraints.

## Scope

- Single BC company, consolidated company-level data only.
- **No global dimensions in scope.** No SITE, no department, no shortcut dimensions on any entity. The 13-week cash flow operates at the highest consolidated level.
- Standard Power Automate Business Central connector for all flows. No custom PA connector required.

### Tenant permission constraints (builder)

- Has OData publish rights (can publish existing BC pages as ODataV4 Web Services).
- Does NOT have AL extension publish permissions (cannot install a custom AL API page).
- Does NOT have Azure AD app registration permissions (cannot stand up the OAuth required for a PA HTTP action to consume a published Web Service).
- Net effect: we work within what Microsoft's published APIs already expose, and derive missing fields in Python downstream.

## Standard PA BC connector behavior

The standard BC connector exposes both the built-in `v2.0` namespace AND any custom APIs registered in the tenant — Microsoft's `microsoft/reportsFinance/beta`, plus any custom AL API pages installed via extension — via its "API category" dropdown. One connector covers all five flows; only the API category and table name change per flow. ODataV4 Web Services published from BC pages do NOT appear in this dropdown; they require an HTTP action with OAuth, which is gated by Azure AD app registration permissions.

## The five flows

### Flow 1 — AR (customer ledger entries, open + recent paid, last 24 months)

- BC source table: Cust. Ledger Entry (T21)
- Standard v2.0: not available
- Primary path: `microsoft/reportsFinance/beta/customerLedgerEntries`
- Status: Beta (live and stable in production usage since BC21 / 2022 Wave 2)
- Confirmed working in tenant: Find records (V3) returned HTTP 200 with `$top=5` on May 27, 2026.
- Fields returned by the endpoint: `entryNumber`, `postingDate`, `documentType`, `description`, `documentNumber`, `externalDocumentNumber`, `balancingAccountNumber`, `balancingAccountType`, `customerNumber`, `open` (boolean), `dimensionSetID`, `currencyCode`, `lastModifiedDateTime`, `amount`, `debitAmount`, `creditAmount`, plus local-currency variants.
- Fields missing vs. T21: `dueDate`, `remainingAmount`, `paymentTermsCode`, `pmtDiscountDate`, `originalAmount`.

#### Derivation plan for missing fields

- `dueDate` → derived in Python as `postingDate + paymentTermsDays`, where `paymentTermsDays` is pulled per customer from the customer master (Flow 4 data). For invoices on standard terms this matches what BC stored at posting. Manually overridden due dates on individual invoices will not be reflected.
- `remainingAmount` → for entries where `open: true`, `amount` is treated as the remaining balance. Overstates open AR wherever partial payments exist. Documented assumption; monitored against actual cash receipts in weekly variance reconciliation.
- `paymentTermsCode` → pulled from customer master (Flow 4) at the customer level, not the invoice level. Effectively the same terms applied across all of a customer's invoices in the model.

### Flow 2 — AP (vendor ledger entries, open + recent paid, last 24 months)

- BC source table: Vendor Ledger Entry (T25)
- Standard v2.0: not available
- Primary path: `microsoft/reportsFinance/beta/vendorLedgerEntries` (to confirm in tenant during Flow 2 build)
- Expected field gaps mirror Flow 1. Same derivation plan applied to vendor side using Flow 4 vendor master.

### Flow 3 — GL entries (current and prior FY for cash, revolver, A/R, A/P accounts)

- BC source table: G/L Entry (T17)
- Standard v2.0: `generalLedgerEntries` available
- Properties exposed: `id`, `entryNumber`, `postingDate`, `documentNumber`, `documentType`, `accountId`, `accountNumber`, `description`, `debitAmount`, `creditAmount`, `additionalCurrencyDebitAmount`, `additionalCurrencyCreditAmount`, `lastModifiedDateTime`.
- Sufficient for consolidated-company cash flow needs. No derivation needed.

### Flow 4 — Customer and Vendor master

- BC source tables: Customer (T18), Vendor (T23), Payment Terms (T3), Currency (T4)
- Standard v2.0: all available
- Primary path: `v2.0/customers`, `v2.0/vendors` with `$expand=paymentTerm, currency, customerFinancialDetails`
- Critical dependency: `paymentTerm` expansion supplies the `paymentTermsDays` value that drives the `dueDate` derivation for Flows 1 and 2. Flow 4 must be functional before the Python transformation layer for Flows 1 and 2 can compute accurate due dates.

### Flow 5 — Open SOs and POs

- BC source tables: Sales Header (T36), Purchase Header (T38), filtered to status=Open
- Standard v2.0: `salesOrders` and `purchaseOrders` available with `$filter=status eq 'Open'`
- No friction expected.

## Risks and accepted limitations

- **Beta endpoint risk (Flows 1 and 2):** `reportsFinance/beta` has been stable in production usage since 2022 Wave 2 but remains officially Beta. Microsoft could change it without a deprecation cycle. Mitigation is bounded by the tenant permission constraints above — we cannot pre-build a custom API page fallback ourselves.
- **API v1.0 removed in BC v28 (2026 Release Wave 1, April 2026):** all flows target v2.0 stable or custom APIs. No v1.0 references anywhere.
- **AR receipts forecast bias:** Open AR is slightly overstated wherever invoices have been partially paid down. Expected small effect for a food manufacturer on mostly Net-30 terms. Monitored in weekly actuals reconciliation.
- **Override visibility on due dates:** Hand-overridden due dates on individual invoices will not be reflected in the derived `dueDate`. Detectable in reconciliation if patterns emerge.

## Future enhancement milestone — "richer AR/AP source"

When admin support becomes available for either an AL API page deployment OR an Azure AD app registration for ODataV4 Web Service consumption, swap Flows 1 and 2 to read directly-exposed `dueDate` and `remainingAmount` fields. Eliminates the derivation logic and the partial-payment overstatement. Independent of this build's critical path; runs whenever IT bandwidth allows.

## What was verified in the BC tenant (Flow 1, May 27 2026)

- `microsoft/reportsFinance/beta` appears in the API category dropdown.
- `customerLedgerEntries` appears in the table dropdown under that API category.
- Find records (V3) action returns HTTP 200 with `$top=5`.
- Pending: same verification for `vendorLedgerEntries` when Flow 2 is built.