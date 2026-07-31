"""Idempotency-key evaluation (pure decision logic; persistence lives in ingest)."""

import enum
from datetime import datetime

from app.models import IdempotencyKey


class IdempotencyVerdict(enum.StrEnum):
    REPLAY = "replay"
    CONFLICT = "conflict"
    EXPIRED = "expired"


def evaluate_existing(
    row: IdempotencyKey, *, content_sha256: str, now: datetime
) -> IdempotencyVerdict:
    if row.expires_at <= now:
        return IdempotencyVerdict.EXPIRED
    if row.content_sha256 == content_sha256:
        return IdempotencyVerdict.REPLAY
    return IdempotencyVerdict.CONFLICT
