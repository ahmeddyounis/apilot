from decimal import Decimal

import pytest

from app.core.money import amount_tolerance, currency_exponent, quantize_amount
from app.core.normalize import normalize_invoice_number, normalize_vendor_name

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("currency", "exponent"), [("USD", 2), ("EUR", 2), ("JPY", 0), ("BHD", 3), ("xyz", 2)]
)
def test_currency_exponent(currency: str, exponent: int) -> None:
    assert currency_exponent(currency) == exponent


def test_quantize_and_tolerance() -> None:
    assert quantize_amount(Decimal("1.005"), "USD") == Decimal("1.01")
    assert quantize_amount(Decimal("1234.4"), "JPY") == Decimal("1234")
    assert quantize_amount(Decimal("1.23456"), "BHD") == Decimal("1.235")
    assert amount_tolerance("USD") == Decimal("0.01")
    assert amount_tolerance("JPY") == Decimal("1")
    assert amount_tolerance("BHD") == Decimal("0.001")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("ACME Office Supply Co.", "acme office supply"),
        ("Acme Office Supply Company", "acme office supply"),
        ("ACME  OFFICE   SUPPLY, LLC", "acme office supply"),
        ("Müller GmbH", "müller"),
        ("Solo", "solo"),
        ("Ltd", "ltd"),  # a lone legal suffix is kept — it's the whole name
    ],
)
def test_normalize_vendor_name(raw: str, expected: str) -> None:
    assert normalize_vendor_name(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("INV-2026-0042", "INV20260042"),
        ("inv 2026 0042", "INV20260042"),
        ("#0001234", "1234"),
        ("0000", "0"),
        ("A/B.C-D", "ABCD"),
    ],
)
def test_normalize_invoice_number(raw: str, expected: str) -> None:
    assert normalize_invoice_number(raw) == expected
