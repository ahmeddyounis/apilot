from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.models import IdempotencyKey
from app.services.idempotency import IdempotencyVerdict, evaluate_existing

pytestmark = pytest.mark.unit

NOW = datetime(2026, 7, 31, 12, 0, 0, tzinfo=UTC)


def make_row(content_sha256: str, expires_in_hours: int) -> IdempotencyKey:
    return IdempotencyKey(
        api_key_id=uuid4(),
        endpoint="POST /api/v1/invoices",
        key="k-1",
        content_sha256=content_sha256,
        invoice_id=uuid4(),
        response_status=202,
        expires_at=NOW + timedelta(hours=expires_in_hours),
    )


def test_same_content_replays() -> None:
    row = make_row("abc", expires_in_hours=1)
    assert evaluate_existing(row, content_sha256="abc", now=NOW) is IdempotencyVerdict.REPLAY


def test_different_content_conflicts() -> None:
    row = make_row("abc", expires_in_hours=1)
    assert (
        evaluate_existing(row, content_sha256="different", now=NOW) is IdempotencyVerdict.CONFLICT
    )


def test_expired_row_is_treated_as_new() -> None:
    row = make_row("abc", expires_in_hours=-1)
    # Even matching content: an expired key no longer replays.
    assert evaluate_existing(row, content_sha256="abc", now=NOW) is IdempotencyVerdict.EXPIRED


def test_expiry_boundary_is_exclusive() -> None:
    row = make_row("abc", expires_in_hours=0)
    assert evaluate_existing(row, content_sha256="abc", now=NOW) is IdempotencyVerdict.EXPIRED
