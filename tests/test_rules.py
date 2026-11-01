from datetime import date
from decimal import Decimal

import pytest

from claimsleuth.rules import (add_months, compute_settlement, coverage_window, find_exclusion,
                               format_inr, is_valid_serial_format, normalise_serial, parse_iso_date,
                               parse_money)


@pytest.mark.parametrize("start, months, expected", [
    (date(2024, 1, 31), 1, date(2024, 2, 29)),    # leap year clamp
    (date(2023, 1, 31), 1, date(2023, 2, 28)),
    (date(2024, 2, 29), 12, date(2025, 2, 28)),
    (date(2025, 10, 31), 12, date(2026, 10, 31)),
    (date(2025, 11, 15), 3, date(2026, 2, 15)),   # year roll-over
])
def test_add_months(start, months, expected):
    assert add_months(start, months) == expected


def test_coverage_window_is_inclusive_on_expiry_day():
    on_expiry = coverage_window(date(2025, 9, 30), date(2026, 9, 30), 12)
    day_after = coverage_window(date(2025, 9, 30), date(2026, 10, 1), 12)
    assert on_expiry["within_window"] and on_expiry["days_from_expiry"] == 0
    assert not day_after["within_window"] and day_after["days_from_expiry"] == -1


def test_coverage_window_flags_incident_before_purchase():
    w = coverage_window(date(2025, 5, 1), date(2025, 4, 30), 24)
    assert w["incident_before_purchase"] and not w["within_window"]


def test_settlement_basic():
    s = compute_settlement(Decimal("18500.00"), Decimal("2500.00"), Decimal("80000.00"), Decimal("0"))
    assert s.payout == Decimal("16000.00") and s.eligible_amount == Decimal("16000.00")
    assert s.currency == "INR"
    assert s.explanation == ("Repair cost ₹18,500.00 - deductible ₹2,500.00 = eligible ₹16,000.00; capped at limit "
                             "₹80,000.00 -> ₹16,000.00; depreciation 0.0% = -₹0.00; payout ₹16,000.00.")


def test_settlement_cap_then_depreciation():
    s = compute_settlement(Decimal("72000.00"), Decimal("5000.00"), Decimal("60000.00"), Decimal("0.05"))
    assert (s.eligible_amount, s.capped_amount, s.depreciation_amount, s.payout) == (
        Decimal("67000.00"), Decimal("60000.00"), Decimal("3000.00"), Decimal("57000.00"))


def test_settlement_cost_below_deductible_is_zero():
    assert compute_settlement(Decimal("2000"), Decimal("4000"), Decimal("125000"), Decimal("0.1")).payout == Decimal("0.00")


def test_settlement_rounds_half_up_to_cents():
    # 100.05 * 0.10 = 10.005 -> 10.01 (half-up; float banker's rounding would give 10.00)
    s = compute_settlement(Decimal("100.05"), Decimal("0"), Decimal("1000"), Decimal("0.10"))
    assert s.depreciation_amount == Decimal("10.01") and s.payout == Decimal("90.04")


@pytest.mark.parametrize("value, text", [("0", "₹0.00"), ("7.5", "₹7.50"), ("999", "₹999.00"), ("1000", "₹1,000.00"),
                                         ("16000", "₹16,000.00"), ("125000", "₹1,25,000.00"),
                                         ("12345678.9", "₹1,23,45,678.90"), ("-3000", "-₹3,000.00")])
def test_format_inr_uses_indian_grouping(value, text):
    assert format_inr(Decimal(value)) == text
    assert parse_money(text.lstrip("-")) == abs(Decimal(value)).quantize(Decimal("0.01"))


def test_serial_rules():
    assert normalise_serial("  nmb-x15-998821 ") == "NMB-X15-998821"
    assert is_valid_serial_format("NMB-X15-998821")
    assert not is_valid_serial_format("AB")            # too short
    assert not is_valid_serial_format("NMB X15 998821")  # spaces
    assert not is_valid_serial_format("-NMB-X15")       # must start alphanumeric


@pytest.mark.parametrize("text, expected", [
    ("Corrosion consistent with water damage from a spilled drink.", "water damage"),
    ("Power IC failed. No water damage observed.", None),
    ("Failure not caused by water damage.", None),
    ("Board is free of water damage.", None),
    ("No dents. Signs of water damage on the keyboard.", "water damage"),
    ("Keyboard controller failure.", None),
])
def test_find_exclusion_respects_negation(text, expected):
    assert find_exclusion(text, ["water damage", "cosmetic scratches"]) == expected


def test_parsers():
    assert parse_money("₹1,25,000.00") == Decimal("125000.00")          # Indian digit grouping
    assert parse_money("INR 18,900.00") == Decimal("18900.00")
    assert parse_money("Rs. 7,650") == Decimal("7650.00")
    assert parse_money("Estimated: 99.5") == Decimal("99.50")
    assert parse_money("n/a") is None
    assert parse_money("$1,100.00") is None and parse_money("USD 540.00") is None   # never read as rupees
    assert parse_iso_date("2026-02-30") is None
    assert parse_iso_date(" 2026-02-28 ") == date(2026, 2, 28)
