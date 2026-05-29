"""Tests for src/transform/ap_due_dates.py."""
import datetime as dt

import pandas as pd

from src.transform.ap_due_dates import (
    DEFAULT_DUE_DAYS,
    build_vendor_due_days,
    stamp_due_dates,
)


def test_build_vendor_due_days_resolves_and_falls_back() -> None:
    """Vendors with valid terms resolve to the parsed day count; otherwise default."""
    vendors = pd.DataFrame([
        {"number": "1001", "paymentTermsId": "terms-net30"},
        {"number": "1002", "paymentTermsId": "terms-net15"},
        {"number": "4843", "paymentTermsId": "terms-unknown"},
        {"number": "1938", "paymentTermsId": ""},
    ])
    terms = pd.DataFrame([
        {"id": "terms-net30", "dueDateCalculation": "30D"},
        {"id": "terms-net15", "dueDateCalculation": "15D"},
        {"id": "terms-cm", "dueDateCalculation": "CM"},
    ])

    lookup = build_vendor_due_days(vendors, terms)

    assert lookup["1001"] == 30
    assert lookup["1002"] == 15
    assert lookup["4843"] == DEFAULT_DUE_DAYS
    assert lookup["1938"] == DEFAULT_DUE_DAYS


def test_build_vendor_due_days_uses_default_for_calendar_relative_terms() -> None:
    """CM, CM+10D etc. fall through to None -> default."""
    vendors = pd.DataFrame([
        {"number": "9001", "paymentTermsId": "terms-cm"},
        {"number": "9002", "paymentTermsId": "terms-cm10"},
    ])
    terms = pd.DataFrame([
        {"id": "terms-cm", "dueDateCalculation": "CM"},
        {"id": "terms-cm10", "dueDateCalculation": "CM+10D"},
    ])

    lookup = build_vendor_due_days(vendors, terms)

    assert lookup["9001"] == DEFAULT_DUE_DAYS
    assert lookup["9002"] == DEFAULT_DUE_DAYS


def test_stamp_due_dates_adds_due_date_columns() -> None:
    """dueDate = postingDate + dueDays for each open AP entry."""
    ap = pd.DataFrame([
        {"vendorNumber": "1001", "postingDate": "2026-05-01", "amount": -1000.0},
        {"vendorNumber": "1002", "postingDate": "2026-04-15", "amount": -5000.0},
    ])
    lookup = {"1001": 30, "1002": 15}

    stamped = stamp_due_dates(ap, lookup)

    assert stamped.loc[0, "dueDays"] == 30
    assert stamped.loc[0, "dueDate"] == dt.date(2026, 5, 31)
    assert stamped.loc[1, "dueDays"] == 15
    assert stamped.loc[1, "dueDate"] == dt.date(2026, 4, 30)
    assert stamped.loc[0, "amount"] == -1000.0


def test_stamp_due_dates_defaults_unknown_vendor() -> None:
    """A vendor missing from the lookup falls back to default_days."""
    ap = pd.DataFrame([
        {"vendorNumber": "99999", "postingDate": "2026-05-01", "amount": -1000.0},
    ])
    lookup = {"1001": 30}

    stamped = stamp_due_dates(ap, lookup)

    assert stamped.loc[0, "dueDays"] == DEFAULT_DUE_DAYS
    assert stamped.loc[0, "dueDate"] == dt.date(2026, 5, 31)


def test_stamp_due_dates_sign_convention_agnostic() -> None:
    """Due-date stamping works for all AP document types regardless of amount sign."""
    ap = pd.DataFrame([
        {"vendorNumber": "1001", "postingDate": "2026-05-01", "amount": -1000.0,
         "documentType": "Invoice"},
        {"vendorNumber": "1001", "postingDate": "2026-05-01", "amount": 500.0,
         "documentType": "Payment"},
        {"vendorNumber": "1001", "postingDate": "2026-05-01", "amount": 250.0,
         "documentType": "Credit Memo"},
    ])
    lookup = {"1001": 30}

    stamped = stamp_due_dates(ap, lookup)

    expected = dt.date(2026, 5, 31)
    assert (stamped["dueDate"] == expected).all()
    assert (stamped["dueDays"] == 30).all()