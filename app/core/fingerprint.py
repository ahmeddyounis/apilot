"""Payment-detail fingerprinting.

The extraction wire carries the verbatim remittance block (`payment_details_raw`)
just long enough to compute a keyed hash here; the raw text is never persisted.

Identity = the set of long digit runs (account numbers, routing codes, the numeric
body of an IBAN) after stripping ALL punctuation/whitespace — so formatting, case,
labels, and grouping can never change the fingerprint. Bank-code letters inside
IBANs are deliberately ignored; the digits alone are a stable, sufficient signal.
"""

import hashlib
import hmac
import re

_NON_ALNUM = re.compile(r"[^A-Z0-9]")
_DIGIT_RUN = re.compile(r"\d{6,}")


def payment_fingerprint(raw_details: str, *, secret: str) -> str | None:
    """HMAC-SHA256 over the normalized account-number digit runs, or None if the
    text contains nothing identifying."""
    compact = _NON_ALNUM.sub("", raw_details.upper())
    identifiers = sorted(set(_DIGIT_RUN.findall(compact)))
    if not identifiers:
        return None
    material = "|".join(identifiers)
    return hmac.new(secret.encode(), material.encode(), hashlib.sha256).hexdigest()
