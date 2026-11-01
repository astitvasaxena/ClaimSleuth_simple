"""Deterministic warranty rules: dates, serial numbers, exclusions and settlement.

These functions are the only place where ClaimSleuth does date or money arithmetic.
No LLM is involved; the same input always gives the same output.
"""
from __future__ import annotations

import calendar
import re
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from claimsleuth.schemas import Settlement

CENT = Decimal("0.01")
SERIAL_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9-]{4,29}$")
_MONEY_PATTERN = re.compile(r"(?:₹|INR|Rs\.?)?\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)", re.I)
# Amounts are Indian Rupees; an amount marked in another currency is unreadable, never silently taken as INR.
_FOREIGN_CURRENCY = re.compile(r"[$€£¥]|\b(USD|EUR|GBP)\b", re.I)
_NEGATION_BEFORE = re.compile(r"\b(no|not|without|never|free of)\b[\w\s,/-]{0,20}$")


# --------------------------------------------------------------------------- dates

def parse_iso_date(text: str) -> date | None:
    try:
        return datetime.strptime(text.strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def add_months(start: date, months: int) -> date:
    """Calendar month addition; a day that does not exist is clamped to the month's last day
    (2024-01-31 + 1 month = 2024-02-29), the same behaviour as dateutil.relativedelta."""
    month_index = start.month - 1 + months
    year = start.year + month_index // 12
    month = month_index % 12 + 1
    day = min(start.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def coverage_window(purchase: date, incident: date, duration_months: int) -> dict:
    """Coverage runs from the purchase date to purchase + duration, both days inclusive."""
    expiry = add_months(purchase, duration_months)
    return {
        "purchase_date": purchase.isoformat(),
        "incident_date": incident.isoformat(),
        "expiry_date": expiry.isoformat(),
        "incident_before_purchase": incident < purchase,
        "within_window": purchase <= incident <= expiry,
        "days_from_expiry": (expiry - incident).days,   # negative = incident after expiry
    }


# --------------------------------------------------------------------------- serial numbers

def normalise_serial(serial: str | None) -> str:
    return (serial or "").strip().upper()


def is_valid_serial_format(serial: str) -> bool:
    return bool(SERIAL_PATTERN.fullmatch(normalise_serial(serial)))


# --------------------------------------------------------------------------- exclusions

def find_exclusion(text: str, exclusions: list[str]) -> str | None:
    """Return the first policy exclusion phrase that appears in the text and is not negated
    (so 'No water damage observed' does not match 'water damage')."""
    lowered = text.lower()
    for phrase in exclusions:
        needle = phrase.lower()
        start = lowered.find(needle)
        while start != -1:
            if not _NEGATION_BEFORE.search(lowered[:start]):
                return phrase
            start = lowered.find(needle, start + 1)
    return None


# --------------------------------------------------------------------------- money

def parse_money(text: str) -> Decimal | None:
    """An INR amount such as '₹1,25,000.00', 'INR 18,900.00' or '12500'; None if unreadable or in another currency."""
    if _FOREIGN_CURRENCY.search(text):
        return None
    match = _MONEY_PATTERN.search(text)
    if not match:
        return None
    try:
        return Decimal(match.group(1).replace(",", "")).quantize(CENT, rounding=ROUND_HALF_UP)
    except InvalidOperation:
        return None


def _cents(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def format_inr(value: Decimal) -> str:
    """Indian digit grouping: Decimal('125000') -> '₹1,25,000.00'."""
    amount = _cents(Decimal(value))
    whole, frac = f"{abs(amount):.2f}".split(".")
    head, tail = whole[:-3], whole[-3:]
    groups = []
    while len(head) > 2:
        head, groups = head[:-2], [head[-2:], *groups]
    grouped = ",".join(([head] if head else []) + groups + [tail])
    return f"{'-' if amount < 0 else ''}₹{grouped}.{frac}"


def compute_settlement(repair_cost: Decimal, deductible: Decimal, coverage_limit: Decimal,
                       depreciation_rate: Decimal) -> Settlement:
    """payout = min(max(cost - deductible, 0), limit) * (1 - depreciation), rounded half-up to cents."""
    cost = _cents(max(Decimal("0"), Decimal(repair_cost)))
    ded = _cents(max(Decimal("0"), Decimal(deductible)))
    limit = _cents(max(Decimal("0"), Decimal(coverage_limit)))
    rate = min(Decimal("1"), max(Decimal("0"), Decimal(depreciation_rate)))

    eligible = _cents(max(Decimal("0"), cost - ded))
    capped = min(eligible, limit)
    depreciation = _cents(capped * rate)
    payout = _cents(capped - depreciation)

    explanation = (
        f"Repair cost {format_inr(cost)} - deductible {format_inr(ded)} = eligible {format_inr(eligible)}; "
        f"capped at limit {format_inr(limit)} -> {format_inr(capped)}; "
        f"depreciation {rate * 100:.1f}% = -{format_inr(depreciation)}; payout {format_inr(payout)}."
    )
    return Settlement(
        repair_cost=cost, deductible=ded, eligible_amount=eligible, coverage_limit=limit,
        capped_amount=capped, depreciation_rate=rate, depreciation_amount=depreciation,
        payout=payout, explanation=explanation,
    )
