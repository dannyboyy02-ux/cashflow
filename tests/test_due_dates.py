"""Tests for src/transform/due_dates.py."""
import datetime as dt

import pandas as pd
import pytest

from src.transform.due_dates import (
    DEFAULT_DUE_DAYS,
    build_customer_due_days,
    parse_due_date_calculation,
    stamp_due_dates,
)


@pytest.mark.parametrize(
    "formula,expected",
    [
        ("30D", 30),
        ("10D", 10),
        ("1D", 1),
        ("60D", 60),
        ("2W", 14),
        ("1M", 30),
        ("1Y", 365),
        ("", None),
        (None, None),
        ("CM", None),
        ("CM+10D", None),
        ("garbage", None),
    ],
)
def test_parse_due_date_calculation(formula, expected):
    assert parse_due_date_calculation(formula) == expected


def test_parse_handles_pandas_na():
    assert parse_due_date_calculation(pd.NA) is None


def _terms_df():
    return pd.DataFrame(
        {
            "id": [
                "b10fe290-5f6a-eb11-aa81-000d3afcdb74",
                "c17fdc51-5f6a-eb11-aa81-000d3afcdb74",
                "caf8d2d2-5f6a-eb11-aa81-000d3afcdb74",
            ],
            "dueDateCalculation": ["30D", "10D", "1D"],
        }
    )


def _customers_df():
    return pd.DataFrame(
        {
            "number": ["CUST-B", "5000-B", "3331-B", "9999-B"],
            "paymentTermsId": [
                "b10fe290-5f6a-eb11-aa81-000d3afcdb74",
                "c17fdc51-5f6a-eb11-aa81-000d3afcdb74",
                "00000000-0000-0000-0000-000000000000",
                "deadbeef-0000-0000-0000-000000000000",
            ],
        }
    )


def test_build_customer_due_days_resolves_and_falls_back():
    lookup = build_customer_due_days(_customers_df(), _terms_df())
    assert lookup["CUST-B"] == 30
    assert lookup["5000-B"] == 10
    assert lookup["3331-B"] == DEFAULT_DUE_DAYS
    assert lookup["9999-B"] == DEFAULT_DUE_DAYS


def test_stamp_due_dates_adds_due_date_columns():
    lookup = build_customer_due_days(_customers_df(), _terms_df())
    ar = pd.DataFrame(
        {
            "customerNumber": ["CUST-B", "5000-B", "3331-B"],
            "postingDate": ["2024-09-21", "2024-09-21", "2024-09-21"],
            "documentType": ["Invoice", "Invoice", "Credit Memo"],
            "amount": [1000.0, 500.0, -200.0],
        }
    )

    out = stamp_due_dates(ar, lookup)

    assert out.loc[0, "dueDays"] == 30
    assert out.loc[0, "dueDate"] == dt.date(2024, 10, 21)
    assert out.loc[1, "dueDays"] == 10
    assert out.loc[1, "dueDate"] == dt.date(2024, 10, 1)
    assert out.loc[2, "dueDays"] == DEFAULT_DUE_DAYS
    assert out.loc[2, "dueDate"] == dt.date(2024, 10, 21)


def test_stamp_due_dates_defaults_unknown_customer():
    lookup = {"CUST-B": 30}
    ar = pd.DataFrame(
        {"customerNumber": ["NOT-IN-MASTER"], "postingDate": ["2024-09-21"]}
    )

    out = stamp_due_dates(ar, lookup)

    assert out.loc[0, "dueDays"] == DEFAULT_DUE_DAYS
    assert out.loc[0, "dueDate"] == dt.date(2024, 10, 21)