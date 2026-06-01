"""Project term-loan debt service into two weekly cash streams.

Latitude carries 5 term loans, paying principal + interest on the 1st of each
month (shifted to the next business day when the 1st is a weekend/federal
holiday). Treasury tracks principal as its own category, separate from A/P;
interest is a separate cash item. Both are manually-maintained assumptions read
from a gitignored JSON input (real figures, public repo):

    inputs/debt_schedule.json
    {"loans": [{"name", "balance_outstanding", "annual_interest_rate",
                "monthly_principal_payment", "next_payment_date"}, ...]}

For each loan we amortize forward from next_payment_date until the loan is paid
off or the (unshifted) scheduled date passes the 13-week horizon:

    while balance > 0 and payment_date <= horizon_end:
        interest  = round(balance * annual_interest_rate / 12, 2)   # on opening balance
        principal = min(monthly_principal_payment, balance)         # capped at remaining
        emit (shift_to_business_day(payment_date), loan, principal, interest)
        balance  -= principal
        payment_date = same_day_next_month(payment_date)

Emitted payments are bucketed onto the bucketing Monday-anchored 13-week grid
(reused, not re-derived) into two tables at loan-name granularity:

    debt_principal_by_week (..., loan_name, principal, source_stream="debt_principal")
    debt_interest_by_week  (..., loan_name, interest,  source_stream="debt_interest")

Single extract+calc module (mirrors payroll.py). Deliberately does NOT touch the
Excel writer, snapshot, or combined_disbursements_by_week -- both are separate
forecast lines, and rendering/variance come in a later phase. Graceful: a missing
input file logs a WARNING and writes empty streams so the pipeline still runs.
"""
from __future__ import annotations

import calendar
import datetime as dt
import json
import logging
from pathlib import Path
from typing import Optional

import holidays
import pandas as pd

from src.config import LOG_LEVEL, LOG_FORMAT, INPUTS_DIR, FORECAST_HORIZON_WEEKS
from src.db import get_connection
from src.calc.bucketing import monday_of_week, assign_forecast_week

logger = logging.getLogger(__name__)

INPUT_FILE = INPUTS_DIR / "debt_schedule.json"

PRINCIPAL_TABLE = "debt_principal_by_week"
INTEREST_TABLE = "debt_interest_by_week"
SOURCE_PRINCIPAL = "debt_principal"
SOURCE_INTEREST = "debt_interest"

PRINCIPAL_COLUMNS = ["forecast_week", "week_start_date", "loan_name", "principal", "source_stream"]
INTEREST_COLUMNS = ["forecast_week", "week_start_date", "loan_name", "interest", "source_stream"]


def load_debt_schedule(path: Optional[Path] = None) -> Optional[dict]:
    """Read the debt schedule JSON, or return None if the file is absent."""
    path = Path(path) if path is not None else INPUT_FILE
    if not path.exists():
        return None
    with open(path) as f:
        return json.load(f)


def shift_to_business_day(d: dt.date, holidays_set) -> dt.date:
    """Advance d to the next weekday that isn't a federal holiday."""
    while d.weekday() >= 5 or d in holidays_set:   # Sat=5, Sun=6
        d += dt.timedelta(days=1)
    return d


def same_day_next_month(d: dt.date) -> dt.date:
    """Return the same day-of-month one month later (clamped to month length)."""
    year = d.year + (d.month // 12)
    month = d.month % 12 + 1
    last_day = calendar.monthrange(year, month)[1]
    return dt.date(year, month, min(d.day, last_day))


def generate_payments(loans: list[dict], horizon_end: dt.date, holidays_set) -> list[dict]:
    """Amortize each loan forward, emitting one payment dict per scheduled date.

    The loop bound is on the UNSHIFTED scheduled date (payment_date <= horizon_end);
    the emitted payment_date is the business-day-shifted actual cash date. Interest
    accrues on the opening balance; principal is capped at the remaining balance.
    """
    payments: list[dict] = []
    for loan in loans:
        balance = float(loan["balance_outstanding"])
        rate = float(loan["annual_interest_rate"])
        monthly = float(loan["monthly_principal_payment"])
        name = loan["name"]
        pay_date = dt.date.fromisoformat(loan["next_payment_date"])

        while balance > 0 and pay_date <= horizon_end:
            interest = round(balance * rate / 12, 2)
            principal = min(monthly, balance)
            actual_date = shift_to_business_day(pay_date, holidays_set)
            payments.append({
                "loan_name": name,
                "payment_date": actual_date,
                "principal": round(principal, 2),
                "interest": interest,
            })
            balance -= principal
            pay_date = same_day_next_month(pay_date)
    return payments


def _empty_principal() -> pd.DataFrame:
    return pd.DataFrame(columns=PRINCIPAL_COLUMNS)


def _empty_interest() -> pd.DataFrame:
    return pd.DataFrame(columns=INTEREST_COLUMNS)


def build_debt_by_week(
    schedule: dict,
    as_of: dt.date,
    horizon_weeks: int = FORECAST_HORIZON_WEEKS,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Generate payments and bucket them into (principal_df, interest_df) by week."""
    loans = schedule.get("loans") or []
    week_1_monday = monday_of_week(as_of)
    horizon_end = week_1_monday + dt.timedelta(weeks=horizon_weeks) - dt.timedelta(days=1)
    us_holidays = holidays.UnitedStates(years=[as_of.year, as_of.year + 1])

    payments = generate_payments(loans, horizon_end, us_holidays)
    if not payments:
        return _empty_principal(), _empty_interest()

    pdf = pd.DataFrame(payments)
    stamped = assign_forecast_week(pdf, as_of, date_col="payment_date")
    in_horizon = stamped[
        (stamped["forecast_week"] >= 1) & (stamped["forecast_week"] <= horizon_weeks)
    ]

    principal = (
        in_horizon.groupby(["loan_name", "forecast_week", "week_start_date"], as_index=False)["principal"].sum()
    )
    principal["source_stream"] = SOURCE_PRINCIPAL
    interest = (
        in_horizon.groupby(["loan_name", "forecast_week", "week_start_date"], as_index=False)["interest"].sum()
    )
    interest["source_stream"] = SOURCE_INTEREST

    return principal[PRINCIPAL_COLUMNS], interest[INTEREST_COLUMNS]


def write_to_sqlite(df: pd.DataFrame, table_name: str) -> int:
    """Replace the table contents with df. Returns the number of rows written."""
    with get_connection() as conn:
        df.to_sql(table_name, conn, if_exists="replace", index=False)
    return len(df)


def run(as_of: Optional[dt.date] = None) -> None:
    """Entrypoint: read the schedule, amortize, bucket, write both streams."""
    logging.basicConfig(level=LOG_LEVEL, format=LOG_FORMAT)
    if as_of is None:
        as_of = dt.date.today()

    schedule = load_debt_schedule()
    if schedule is None:
        logger.warning(
            "Debt schedule %s not found; writing empty %s / %s (no debt service this run).",
            INPUT_FILE, PRINCIPAL_TABLE, INTEREST_TABLE,
        )
        write_to_sqlite(_empty_principal(), PRINCIPAL_TABLE)
        write_to_sqlite(_empty_interest(), INTEREST_TABLE)
        return

    principal_df, interest_df = build_debt_by_week(schedule, as_of)
    n_p = write_to_sqlite(principal_df, PRINCIPAL_TABLE)
    n_i = write_to_sqlite(interest_df, INTEREST_TABLE)
    logger.info(
        "Debt service: %d loan(s); principal rows=%d sum=%.2f -> %s; "
        "interest rows=%d sum=%.2f -> %s",
        len(schedule.get("loans") or []),
        n_p, round(float(principal_df["principal"].sum()), 2) if n_p else 0.0, PRINCIPAL_TABLE,
        n_i, round(float(interest_df["interest"].sum()), 2) if n_i else 0.0, INTEREST_TABLE,
    )


if __name__ == "__main__":
    run()
