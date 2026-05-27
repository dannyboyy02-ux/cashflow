# Cash Flow Forecasting Workbook

13-week cash flow forecasting engine. D365 Business Central is the source of truth; Power Automate extracts to OneDrive; Python transforms and calculates; Excel renders; AI augments via the Anthropic API. Calculation logic lives in Python — Excel is the rendering layer only. Every Power Automate flow is replaceable by a manual BC report export (graceful degradation by design).

## Setup

```
cd C:\Projects\cashflow
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
```

Then edit `.env` with real values.

## Repository scope

This repository contains generic methodology, architecture patterns, and code structure as portable professional development. Company-specific data — credentials, BC environment IDs, customer and vendor names, account numbers, financial amounts, revolver mechanics — is kept in untracked local files (`.env`, `data/`, `*.xlsx`). The deployed workbook itself is the operating company's IP; this repository is the methodology that built it.