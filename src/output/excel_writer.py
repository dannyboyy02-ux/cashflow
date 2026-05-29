"""Render the 13-week cash forecast into a CFO-facing Excel workbook.

This is the first hand-off artifact: a single .xlsx the CFO can open and read
without touching the pipeline. It reads the two bucketed tables produced by
src.calc.bucketing (ar_receipts_by_week, ap_disbursements_by_week) plus the
customer/vendor masters for display names, and writes four sheets:

  1. "13-Week Forecast"   -- the headline week-by-week cash view
  2. "AR by Customer"     -- receipts pivoted to customer x week
  3. "AP by Vendor"       -- disbursements pivoted to vendor x week
  4. "Assumptions & Notes"-- as-of date, refresh stamp, methodology, caveats

FORMULAS vs VALUES:

The model's outputs -- AR Receipts, AP Disbursements, Net Cash Flow, and the
totals row -- are written as computed VALUES. They are facts produced by the
calc layer; there's nothing for Excel to recompute.

The Beginning/Ending cash chain is the one place we use live Excel formulas,
because it depends on a number the model does NOT know: the actual bank balance
at the start of week 1. The CFO hand-enters that into the Beginning Cash cell
of week 1; each week's Ending Cash = Beginning + Net Cash Flow, and the next
week's Beginning Cash = the prior week's Ending. Editing the one starting
balance cascades the whole 13-week cash position.
"""
from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path
from typing import Optional

import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from src.config import (
    LOG_LEVEL,
    LOG_FORMAT,
    DATA_DIR,
    ONEDRIVE_DATA_PATH,
    FORECAST_HORIZON_WEEKS,
)
from src.db import get_connection
from src.calc.bucketing import (
    AR_OUTPUT_TABLE,
    AP_OUTPUT_TABLE,
    monday_of_week,
)

logger = logging.getLogger(__name__)

OUTPUT_FILENAME = "cashflow_forecast.xlsx"

SHEET_FORECAST = "13-Week Forecast"
SHEET_AR = "AR by Customer"
SHEET_AP = "AP by Vendor"
SHEET_NOTES = "Assumptions & Notes"

# Currency, no decimals, negatives in red parentheses.
CURRENCY_FMT = "#,##0_);[Red](#,##0)"
DATE_FMT = "yyyy-mm-dd"

_HEADER_FONT = Font(bold=True)
_HEADER_FILL = PatternFill(fill_type="solid", fgColor="D9D9D9")  # light gray
_TITLE_FONT = Font(bold=True, size=14)


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------


def load_table(name: str) -> pd.DataFrame:
    """Read an entire SQLite table into a DataFrame."""
    with get_connection() as conn:
        return pd.read_sql(f"SELECT * FROM {name}", conn)


def load_inputs() -> dict[str, pd.DataFrame]:
    """Load the four source tables the writer needs."""
    return {
        "receipts": load_table(AR_OUTPUT_TABLE),
        "disbursements": load_table(AP_OUTPUT_TABLE),
        "customers": load_table("bc_customers"),
        "vendors": load_table("bc_vendors"),
    }


def latest_refresh_timestamp(folder) -> Optional[dt.datetime]:
    """Return the mtime of the newest CSV in the OneDrive folder, or None.

    Used purely for the "source data refreshed" line on the notes sheet. Returns
    None (rather than raising) if the folder is missing or holds no CSVs, so the
    workbook still renders in environments without the live OneDrive mount.
    """
    folder = Path(folder)
    if not folder.exists():
        return None
    csvs = list(folder.glob("*.csv"))
    if not csvs:
        return None
    newest = max(csvs, key=lambda p: p.stat().st_mtime)
    return dt.datetime.fromtimestamp(newest.stat().st_mtime)


# ---------------------------------------------------------------------------
# Styling helpers
# ---------------------------------------------------------------------------


def _style_header_row(ws: Worksheet, n_cols: int, row: int = 1) -> None:
    """Bold + light-gray fill across the first n_cols cells of a header row."""
    for c in range(1, n_cols + 1):
        cell = ws.cell(row=row, column=c)
        cell.font = _HEADER_FONT
        cell.fill = _HEADER_FILL
        cell.alignment = Alignment(horizontal="center")


def _currency(cell) -> None:
    cell.number_format = CURRENCY_FMT


def _wk_sums(df: pd.DataFrame, value_col: str) -> dict[int, float]:
    """Per-forecast-week sum of value_col as a {week: total} dict."""
    if df.empty or "forecast_week" not in df.columns:
        return {}
    return df.groupby("forecast_week")[value_col].sum().to_dict()


# ---------------------------------------------------------------------------
# Sheet 1 -- 13-Week Forecast
# ---------------------------------------------------------------------------


def _build_forecast_sheet(
    ws: Worksheet,
    receipts: pd.DataFrame,
    disbursements: pd.DataFrame,
    as_of_date: dt.date,
    horizon_weeks: int,
) -> None:
    headers = [
        "Forecast Week",
        "Week Start",
        "Beginning Cash",
        "AR Receipts",
        "AP Disbursements",
        "Net Cash Flow",
        "Ending Cash",
    ]
    ws.append(headers)
    _style_header_row(ws, len(headers))

    week_1_monday = monday_of_week(as_of_date)
    rec = _wk_sums(receipts, "receipts")
    dis = _wk_sums(disbursements, "disbursements")

    tot_rec = tot_dis = tot_net = 0.0
    for i in range(horizon_weeks):
        wk = i + 1
        r = i + 2  # worksheet row (1 = header)
        week_start = week_1_monday + dt.timedelta(days=7 * i)
        rec_v = float(rec.get(wk, 0.0))
        dis_v = float(dis.get(wk, 0.0))
        net_v = rec_v - dis_v
        tot_rec += rec_v
        tot_dis += dis_v
        tot_net += net_v

        ws.cell(row=r, column=1, value=wk)
        date_cell = ws.cell(row=r, column=2, value=week_start)
        date_cell.number_format = DATE_FMT
        # Beginning Cash: week 1 is the CFO's hand-entered starting balance
        # (0 placeholder); later weeks chain off the prior week's Ending Cash.
        if wk == 1:
            begin_cell = ws.cell(row=r, column=3, value=0)
        else:
            begin_cell = ws.cell(row=r, column=3, value=f"=G{r - 1}")
        ws.cell(row=r, column=4, value=rec_v)
        ws.cell(row=r, column=5, value=dis_v)
        ws.cell(row=r, column=6, value=net_v)
        # Ending Cash = Beginning + Net (live formula so the CFO's starting
        # balance cascades forward through the horizon).
        end_cell = ws.cell(row=r, column=7, value=f"=C{r}+F{r}")

        for col in (3, 4, 5, 6, 7):
            _currency(ws.cell(row=r, column=col))

    # Totals row.
    tr = horizon_weeks + 2
    ws.cell(row=tr, column=1, value="TOTAL").font = _HEADER_FONT
    ws.cell(row=tr, column=4, value=tot_rec)
    ws.cell(row=tr, column=5, value=tot_dis)
    ws.cell(row=tr, column=6, value=tot_net)
    for col in (4, 5, 6):
        cell = ws.cell(row=tr, column=col)
        _currency(cell)
        cell.font = _HEADER_FONT

    ws.freeze_panes = "A2"
    _set_widths(ws, {"A": 14, "B": 12, "C": 16, "D": 16, "E": 18, "F": 16, "G": 16})


# ---------------------------------------------------------------------------
# Sheets 2 & 3 -- entity x week pivots
# ---------------------------------------------------------------------------


def _build_entity_sheet(
    ws: Worksheet,
    df: pd.DataFrame,
    master: pd.DataFrame,
    entity_col: str,
    value_col: str,
    number_header: str,
    name_header: str,
    horizon_weeks: int,
) -> None:
    """One row per entity with any in-horizon cash, weeks across, sorted by Total.

    Generic for both AR (customer/receipts) and AP (vendor/disbursements).
    """
    headers = (
        [number_header, name_header]
        + [f"Week {w}" for w in range(1, horizon_weeks + 1)]
        + ["Total"]
    )
    ws.append(headers)
    _style_header_row(ws, len(headers))

    name_map: dict = {}
    if not master.empty and "number" in master.columns and "displayName" in master.columns:
        name_map = dict(zip(master["number"], master["displayName"]))

    if not df.empty and "forecast_week" in df.columns:
        pivot = df.pivot_table(
            index=entity_col,
            columns="forecast_week",
            values=value_col,
            aggfunc="sum",
            fill_value=0.0,
        )
        built = []
        for ent in pivot.index:
            week_vals = [
                float(pivot.loc[ent, w]) if w in pivot.columns else 0.0
                for w in range(1, horizon_weeks + 1)
            ]
            built.append((ent, name_map.get(ent, ""), week_vals, sum(week_vals)))
        built.sort(key=lambda x: x[3], reverse=True)

        for ent, name, week_vals, total in built:
            row = [ent, name] + week_vals + [total]
            ws.append(row)

        # Currency-format the numeric columns (3 .. last) on every data row.
        last_col = len(headers)
        for r in range(2, ws.max_row + 1):
            for c in range(3, last_col + 1):
                _currency(ws.cell(row=r, column=c))

    ws.freeze_panes = "C2"  # keep number + name + header visible while scrolling
    widths = {"A": 16, "B": 32}
    for w in range(1, horizon_weeks + 1):
        widths[get_column_letter(2 + w)] = 12
    widths[get_column_letter(2 + horizon_weeks + 1)] = 16  # Total
    _set_widths(ws, widths)


# ---------------------------------------------------------------------------
# Sheet 4 -- Assumptions & Notes
# ---------------------------------------------------------------------------


def _build_assumptions_sheet(
    ws: Worksheet,
    as_of_date: dt.date,
    refresh_ts: Optional[dt.datetime],
) -> None:
    refresh_str = refresh_ts.isoformat(sep=" ", timespec="seconds") if refresh_ts else "unknown"
    lines: list[tuple[str, bool]] = [
        ("Sample Foods Co. -- 13-Week Cash Flow Forecast", True),
        ("", False),
        (f"As-of date: {as_of_date.isoformat()}", False),
        (f"Source data refreshed: {refresh_str}", False),
        ("", False),
        ("Methodology", True),
        (
            "Receipts and disbursements are timed onto a Monday-anchored 13-week "
            "calendar grid (week 1 = the week containing the as-of date).",
            False,
        ),
        (
            "AR receipts: each open invoice is expected on postingDate + the "
            "customer's empirical DSO (days sales outstanding from trailing "
            "12-month sales). AP disbursements use two streams: (A) payments AP "
            "has already scheduled in Business Central with a future date, taken "
            "verbatim; and (B) open invoices estimated at postingDate + the "
            "vendor's empirical DPO (days payable outstanding).",
            False,
        ),
        (
            "Overdue clamp: any item whose estimated date is already past is "
            "pulled forward to the as-of date (week 1), on the assumption it "
            "collects/pays imminently. Such rows are flagged was_overdue upstream.",
            False,
        ),
        ("", False),
        ("Timing methods (timing_method column upstream)", True),
        ("  ratio          -- empirical DSO/DPO from trailing 12-month activity", False),
        ("  terms_fallback -- contractual payment terms (no trailing activity to measure)", False),
        ("  no_balance     -- master balance <= 0; treated as immediate, clamped to as-of", False),
        ("  master_fallback-- entity missing from the DSO/DPO table; used the row's due-days", False),
        ("  scheduled      -- AP payment pre-loaded in Business Central (Stream A), date as-is", False),
        ("", False),
        ("Caveats", True),
        (
            "This is an automated forecast from current open AR/AP and historical "
            "payment behavior. It does NOT yet include:",
            False,
        ),
        ("  - payroll", False),
        ("  - debt service", False),
        ("  - capital expenditures", False),
        ("  - tax payments", False),
        ("  - new billings beyond current open AR", False),
        ("  - received-but-not-invoiced (RBNI) accruals", False),
        ("", False),
        ("Beginning Cash on the forecast sheet is a placeholder (0). Enter the", False),
        ("actual week-1 starting bank balance and the Ending/Beginning chain", False),
        ("recomputes the full 13-week cash position.", False),
    ]
    for i, (text, is_header) in enumerate(lines, start=1):
        cell = ws.cell(row=i, column=1, value=text)
        if is_header:
            cell.font = _TITLE_FONT if i == 1 else _HEADER_FONT
    ws.column_dimensions["A"].width = 100


# ---------------------------------------------------------------------------
# Assembly + write
# ---------------------------------------------------------------------------


def _set_widths(ws: Worksheet, widths: dict[str, float]) -> None:
    for col, w in widths.items():
        ws.column_dimensions[col].width = w


def build_workbook(
    receipts: pd.DataFrame,
    disbursements: pd.DataFrame,
    customers: pd.DataFrame,
    vendors: pd.DataFrame,
    as_of_date: dt.date,
    refresh_ts: Optional[dt.datetime] = None,
    horizon_weeks: int = FORECAST_HORIZON_WEEKS,
) -> Workbook:
    """Build the four-sheet forecast workbook from the bucketed inputs."""
    wb = Workbook()
    ws1 = wb.active
    ws1.title = SHEET_FORECAST
    _build_forecast_sheet(ws1, receipts, disbursements, as_of_date, horizon_weeks)

    ws2 = wb.create_sheet(SHEET_AR)
    _build_entity_sheet(
        ws2, receipts, customers, "customerNumber", "receipts",
        "Customer Number", "Customer Name", horizon_weeks,
    )

    ws3 = wb.create_sheet(SHEET_AP)
    _build_entity_sheet(
        ws3, disbursements, vendors, "vendorNumber", "disbursements",
        "Vendor Number", "Vendor Name", horizon_weeks,
    )

    ws4 = wb.create_sheet(SHEET_NOTES)
    _build_assumptions_sheet(ws4, as_of_date, refresh_ts)

    return wb


def write_workbook(wb: Workbook, path) -> Path:
    """Save the workbook, creating the parent directory if needed."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path


def run(
    as_of_date: Optional[dt.date] = None,
    output_path: Optional[Path] = None,
) -> Path:
    """Entrypoint: load bucketed tables, build the workbook, write the .xlsx."""
    logging.basicConfig(level=LOG_LEVEL, format=LOG_FORMAT)
    if as_of_date is None:
        as_of_date = dt.date.today()
    if output_path is None:
        output_path = DATA_DIR / OUTPUT_FILENAME

    data = load_inputs()
    refresh_ts = latest_refresh_timestamp(ONEDRIVE_DATA_PATH)
    logger.info(
        "Loaded %d receipt rows, %d disbursement rows; as_of=%s, refreshed=%s",
        len(data["receipts"]), len(data["disbursements"]),
        as_of_date.isoformat(),
        refresh_ts.isoformat(sep=" ", timespec="seconds") if refresh_ts else "unknown",
    )

    wb = build_workbook(
        data["receipts"], data["disbursements"],
        data["customers"], data["vendors"],
        as_of_date, refresh_ts,
    )
    saved = write_workbook(wb, output_path)
    logger.info("Wrote workbook to %s", saved)
    return saved


if __name__ == "__main__":
    run()
