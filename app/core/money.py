"""Currency-aware Decimal helpers.

Tolerances are "one unit in the last place" for the currency's ISO-4217 exponent
(JPY has no minor unit; BHD has three). Shared by extraction arithmetic checks and
the validation rules engine so both layers agree on what "equal" means for money.
"""

from decimal import ROUND_HALF_UP, Decimal

# Exceptions to the default exponent of 2 (minor units per ISO 4217).
_CURRENCY_EXPONENTS = {
    "JPY": 0,
    "KRW": 0,
    "VND": 0,
    "CLP": 0,
    "ISK": 0,
    "BHD": 3,
    "KWD": 3,
    "OMR": 3,
    "TND": 3,
    "JOD": 3,
}


def currency_exponent(currency: str) -> int:
    return _CURRENCY_EXPONENTS.get(currency.upper(), 2)


def quantize_amount(value: Decimal, currency: str) -> Decimal:
    exponent = currency_exponent(currency)
    return value.quantize(Decimal(1).scaleb(-exponent), rounding=ROUND_HALF_UP)


def amount_tolerance(currency: str) -> Decimal:
    """One unit in the currency's last decimal place."""
    return Decimal(1).scaleb(-currency_exponent(currency))
