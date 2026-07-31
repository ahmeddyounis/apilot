"""Audit-row helper. Rows are added to the caller's session/transaction —
an audit entry commits atomically with the change it records."""

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditLog


def record(
    session: AsyncSession,
    *,
    entity_type: str,
    event_type: str,
    actor: str,
    entity_id: UUID | None = None,
    invoice_id: UUID | None = None,
    from_status: str | None = None,
    to_status: str | None = None,
    payload: dict[str, Any] | None = None,
) -> AuditLog:
    entry = AuditLog(
        entity_type=entity_type,
        entity_id=entity_id,
        invoice_id=invoice_id,
        event_type=event_type,
        from_status=from_status,
        to_status=to_status,
        actor=actor,
        payload=payload or {},
    )
    session.add(entry)
    return entry
