"""The payment fingerprint has to be stable for one account and different for two.

F-110 compares this value against a vendor's known accounts, and a flagged account
is no longer trusted automatically — so every unstable fingerprint is an invoice a
human has to clear, and every over-stable one is a payment redirection walking
through.
"""

import pytest

from app.core.fingerprint import payment_fingerprint

pytestmark = pytest.mark.unit

SECRET = "test-secret"
ACCOUNT = "Bank: First National\nRouting: 021000021\nAccount: 987654321"


def fp(text: str) -> str | None:
    return payment_fingerprint(text, secret=SECRET)


@pytest.mark.parametrize(
    "variant",
    [
        "Remit to First National, 021000021 / 987654321",  # merged into one run before
        "ROUTING 021000021, ACCOUNT 987654321",
        "routing: 021-000-021\naccount: 987-654-321",
        "Routing: 021000021 Account: 987654321\nReference: INV-20260731",
        "Routing: 021000021 Account: 987654321\nPlease quote ref 4417029",
        "Routing: 021000021 Account: 987654321\nPlease pay by 010226",
    ],
)
def test_same_account_written_differently_is_one_identity(variant: str) -> None:
    """Formatting and per-invoice references must not look like a bank change."""
    assert fp(variant) == fp(ACCOUNT), variant


@pytest.mark.parametrize(
    "changed",
    [
        "Routing: 021000021 Account: 111222333",  # account swapped, routing kept
        "Routing: 111000025 Account: 987654321",  # routing swapped
        "IBAN GB94 BARC 2020 1530 0934 59",
    ],
)
def test_a_different_account_is_a_different_identity(changed: str) -> None:
    assert fp(changed) != fp(ACCOUNT), changed


def test_iban_grouping_and_case_and_trailing_bic_collapse() -> None:
    variants = [
        "IBAN GB29 NWBK 6016 1331 9268 19",
        "iban: gb29nwbk60161331926819",
        "IBAN: GB29 NWBK 6016 1331 9268 19 (BIC NWBKGB2L)",
        "Please remit to IBAN GB29-NWBK-6016-1331-9268-19",
    ]
    assert len({fp(v) for v in variants}) == 1


def test_short_domestic_accounts_still_produce_a_fingerprint() -> None:
    """Requiring six consecutive digits silently disabled the signal for sort-code
    formats, while payment_details_present stayed true."""
    assert fp("Sort code 40-47-84, Account 70872490") is not None


def test_no_identifying_numbers_is_no_signal() -> None:
    assert fp("Pay by cheque to the address above") is None
