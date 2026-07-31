from datetime import date
from decimal import Decimal

import pytest

from app.core.fingerprint import payment_fingerprint
from app.llm.embeddings import FakeEmbeddings
from app.services.dedup import Flag, combined_dup_score, risk_score, should_hold

pytestmark = pytest.mark.unit

SECRET = "test-secret"


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


# --- payment fingerprints -----------------------------------------------------------


def test_fingerprint_ignores_formatting() -> None:
    a = payment_fingerprint("IBAN DE89 3704 0044 0532 0130 00", secret=SECRET)
    b = payment_fingerprint("iban: de89-3704-0044-0532-0130-00", secret=SECRET)
    assert a is not None
    assert a == b


def test_fingerprint_differs_by_account_and_secret() -> None:
    a = payment_fingerprint("IBAN DE89 3704 0044 0532 0130 00", secret=SECRET)
    b = payment_fingerprint("IBAN DE89 3704 0044 0532 0130 01", secret=SECRET)
    c = payment_fingerprint("IBAN DE89 3704 0044 0532 0130 00", secret="other")
    assert len({a, b, c}) == 3


def test_fingerprint_none_without_identifiers() -> None:
    assert payment_fingerprint("Please pay promptly. Thank you!", secret=SECRET) is None


def test_fingerprint_uses_routing_and_account_runs() -> None:
    a = payment_fingerprint("Routing 021000021, Account 123456789", secret=SECRET)
    b = payment_fingerprint("ACCT 123456789 / ABA 021000021", secret=SECRET)
    assert a is not None
    assert a == b  # same identifiers, order/formatting-independent


# --- fake embeddings ----------------------------------------------------------------


def test_fake_embeddings_deterministic_and_normalized() -> None:
    embedder = FakeEmbeddings()
    first, second = embedder.embed(["acme invoice 507.60"] * 2)
    assert first == second
    assert abs(cosine(first, first) - 1.0) < 1e-9


def test_fake_embeddings_similarity_ordering() -> None:
    embedder = FakeEmbeddings()
    base, near, far = embedder.embed(
        [
            "acme office supply|INV1001|2026-07-05|USD|507.60|widget a-200 10 47.00",
            "acme office supply|INV1001R|2026-07-05|USD|507.60|widget a-200 10 47.00",
            "globex catering|CAT-77|2026-01-01|EUR|92.00|lunch trays 4 23.00",
        ]
    )
    assert cosine(base, near) > 0.85
    assert cosine(base, near) > cosine(base, far)


# --- scoring & routing ---------------------------------------------------------------


def test_combined_dup_score_full_and_partial() -> None:
    full = combined_dup_score(
        cosine=0.95,
        vendor_a="ACME Office Supply Co",
        vendor_b="ACME Office Supply Co.",
        total_a=Decimal("507.60"),
        total_b=Decimal("507.60"),
        date_a=date(2026, 7, 5),
        date_b=date(2026, 7, 6),
    )
    assert full > 0.9

    weak = combined_dup_score(
        cosine=0.86,
        vendor_a="ACME Office Supply Co",
        vendor_b="Globex Catering",
        total_a=Decimal("507.60"),
        total_b=Decimal("92.00"),
        date_a=date(2026, 7, 5),
        date_b=date(2026, 1, 1),
    )
    assert weak < 0.8


def flag(code: str, severity: str) -> Flag:
    return Flag(code=code, severity=severity, explanation="", evidence={})


def test_should_hold_matrix() -> None:
    assert should_hold([flag("DUP-SUSPECT", "high")]) is True
    assert should_hold([flag("F-110", "high")]) is True
    assert should_hold([flag("F-115", "medium"), flag("F-130", "medium")]) is True
    assert should_hold([flag("F-115", "medium")]) is False
    assert should_hold([flag("F-100", "low"), flag("F-120", "low")]) is False
    assert should_hold([]) is False


def test_risk_score_weights_and_cap() -> None:
    assert risk_score([flag("F-100", "low")]) == Decimal("0.050")
    assert risk_score([flag("F-110", "high"), flag("F-130", "medium")]) == Decimal("0.600")
    many = [flag("DUP-SUSPECT", "high"), flag("F-110", "high"), flag("DUP-EXACT-FILE", "high")]
    assert risk_score(many) == Decimal("1.000")
