"""Classifying provider failures.

Separate from ``app/llm/types.py`` because this is the one place that knows the
Anthropic SDK's exception hierarchy; ``types.py`` stays provider-agnostic so a second
provider can be added without touching it.

The distinction that matters is transient versus terminal. A 429 or a 529 says "not
now" and the invoice should be tried again; a 400 says the request itself is wrong and
retrying spends money to reproduce the same failure. Before this, neither was
distinguished and neither was recorded: a hard API error propagated out of the client
*before* the cost-ledger write, so a rate-limit storm produced no ledger row, no
counter, and an invoice sitting in `extracting` with no error surface until the rescue
cron noticed fifteen minutes later.
"""

import anthropic

from app.llm.types import FixtureMissingError, LLMError


class ProviderFailure(LLMError):
    """A provider call that never produced a usable response.

    A plain exception rather than a dataclass: a frozen one cannot have
    ``__traceback__`` assigned, so it blows up the moment it is re-raised through
    another frame — which is exactly what an exception is for.
    """

    def __init__(self, *, reason: str, transient: bool, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.transient = transient
        self.detail = detail


# Order matters: APITimeoutError subclasses APIConnectionError, and RateLimitError
# subclasses APIStatusError, so the specific cases have to be tested first.
def classify_provider_error(exc: BaseException) -> ProviderFailure:
    detail = str(exc)[:500] or type(exc).__name__
    if isinstance(exc, FixtureMissingError):
        return ProviderFailure(reason="fixture_missing", transient=False, detail=detail)
    if isinstance(exc, anthropic.APITimeoutError):
        return ProviderFailure(reason="timeout", transient=True, detail=detail)
    if isinstance(exc, anthropic.RateLimitError):
        return ProviderFailure(reason="rate_limited", transient=True, detail=detail)
    if isinstance(exc, anthropic.InternalServerError):
        # Covers 529 overloaded, which the SDK models as an InternalServerError.
        return ProviderFailure(reason="overloaded", transient=True, detail=detail)
    if isinstance(exc, anthropic.APIConnectionError):
        return ProviderFailure(reason="connection", transient=True, detail=detail)
    if isinstance(exc, anthropic.AuthenticationError | anthropic.PermissionDeniedError):
        # Retrying will not find a different key.
        return ProviderFailure(reason="unauthorized", transient=False, detail=detail)
    if isinstance(exc, anthropic.BadRequestError):
        # Malformed request, oversized document, unsupported media — ours to fix.
        return ProviderFailure(reason="bad_request", transient=False, detail=detail)
    if isinstance(exc, anthropic.APIStatusError):
        return ProviderFailure(reason="api_error", transient=True, detail=detail)
    return ProviderFailure(reason="unknown", transient=True, detail=detail)
