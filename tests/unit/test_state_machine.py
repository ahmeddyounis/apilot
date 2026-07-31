import pytest

from app.models import InvoiceStatus
from app.services.state_machine import ALLOWED, TERMINAL_STATUSES, is_transition_allowed

pytestmark = pytest.mark.unit


def test_every_status_has_an_entry() -> None:
    assert set(ALLOWED) == set(InvoiceStatus)


def test_terminal_statuses() -> None:
    assert {
        InvoiceStatus.EXTRACTION_FAILED,
        InvoiceStatus.REJECTED,
        InvoiceStatus.EXPORTED,
    } == TERMINAL_STATUSES


def test_every_status_reaches_a_terminal_state() -> None:
    """No invoice can get stuck: from any status some terminal state is reachable."""
    for start in InvoiceStatus:
        frontier = {start}
        seen: set[InvoiceStatus] = set()
        while frontier:
            current = frontier.pop()
            if current in seen:
                continue
            seen.add(current)
            frontier.update(ALLOWED[current])
        assert seen & TERMINAL_STATUSES, f"{start} cannot reach a terminal state"


@pytest.mark.parametrize(
    ("from_status", "to_status", "allowed"),
    [
        (InvoiceStatus.RECEIVED, InvoiceStatus.EXTRACTING, True),
        (InvoiceStatus.RECEIVED, InvoiceStatus.APPROVED, False),
        (InvoiceStatus.EXTRACTING, InvoiceStatus.EXTRACTED, True),
        (InvoiceStatus.EXTRACTING, InvoiceStatus.EXTRACTION_FAILED, True),
        (InvoiceStatus.EXTRACTED, InvoiceStatus.MATCHING, True),
        (InvoiceStatus.EXTRACTED, InvoiceStatus.HELD, True),
        (InvoiceStatus.MATCHING, InvoiceStatus.APPROVED, True),
        (InvoiceStatus.MATCHING, InvoiceStatus.REJECTED, False),
        (InvoiceStatus.EXCEPTION, InvoiceStatus.MATCHING, True),
        (InvoiceStatus.HELD, InvoiceStatus.MATCHING, True),
        (InvoiceStatus.APPROVED, InvoiceStatus.EXPORTED, True),
        (InvoiceStatus.EXPORTED, InvoiceStatus.APPROVED, False),
        (InvoiceStatus.EXTRACTION_FAILED, InvoiceStatus.EXTRACTING, False),
        (InvoiceStatus.REJECTED, InvoiceStatus.APPROVED, False),
    ],
)
def test_transition_rules(
    from_status: InvoiceStatus, to_status: InvoiceStatus, allowed: bool
) -> None:
    assert is_transition_allowed(from_status, to_status) is allowed
