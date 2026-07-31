"""Payment-detail fingerprinting.

The extraction wire carries the verbatim remittance block (`payment_details_raw`)
just long enough to compute a keyed hash here; the raw text is never persisted.

Identity is the *exact set* of account identifiers in the block. Deliberately exact
rather than overlapping: a routing or sort code is shared across every account at a
bank, so "any identifier still matches" would let an attacker keep the routing
number, swap the account number, and stay unflagged. Between the two ways to be
wrong, holding a legitimate invoice costs a reviewer a minute and missing a payment
redirection costs the invoice.

That makes noise expensive, so two things are excluded from the set:

* numbers a label marks as references — vendors print a payment reference beside
  the account and it changes every invoice, which would otherwise change the
  fingerprint on every invoice from a vendor doing something entirely normal;
* separators *between* identifiers are preserved, so "021000021 / 987654321" is two
  identifiers, the same two the labelled multi-line form produces. Stripping all
  punctuation first merged them into a single run that no other formatting of the
  same account details would ever reproduce.

Bank-code letters inside IBANs are kept: they are part of the account's identity and
distinguish otherwise-identical numeric bodies across countries.
"""

import hashlib
import hmac
import re

# Identifiers are matched on the text as written, tolerating the grouping vendors
# use ("GB29 NWBK 6016 1331", "40-47-84"), then normalized by dropping separators.
# Matching first and normalizing second keeps the boundary *between* two numbers,
# which stripping punctuation up front destroys — "021000021 / 987654321" became a
# single run no other formatting of the same details would ever produce.
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?:[ -]?[A-Z0-9]{2,4}){2,8}\b")
# A bare account, routing or sort code, possibly written in groups. Four digits
# total catches short domestic formats; fewer is too generic to identify anything.
_NUMBER = re.compile(r"\b\d{2,}(?:[ -]\d{2,}){0,5}\b")
# A number introduced by one of these words is a transaction reference, not an
# account. Missing one costs a spurious hold, not a missed redirection.
_LABELLED_REFERENCE = re.compile(
    r"\b(?:REF|REFERENCE|REFERENCES|INVOICE|INV|QUOTE|QUOTING|ORDER|DATE|DUE|BY|BEFORE)\b"
    r"[^A-Z0-9]{0,4}([A-Z]{0,6}[ -]?\d[\d -]*)"
)


def _normalize(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", value)


def _identifiers(raw_details: str) -> list[str]:
    """Account identifiers in the block, normalized and de-duplicated."""
    text = raw_details.upper()

    referenced = {_normalize(m.group(1)) for m in _LABELLED_REFERENCE.finditer(text)}

    found: set[str] = set()
    remainder = text
    for match in _IBAN.finditer(text):
        found.add(_normalize(match.group(0)))
        remainder = remainder.replace(match.group(0), " ")
    for match in _NUMBER.finditer(remainder):
        value = _normalize(match.group(0))
        if len(value) >= 4:
            found.add(value)

    # A reference may be captured with a prefix ("INV20260731" vs "20260731"), so
    # drop anything a labelled reference contains.
    return sorted(v for v in found if not any(v in ref for ref in referenced))


def payment_fingerprint(raw_details: str, *, secret: str) -> str | None:
    """HMAC-SHA256 over the account identifiers, or None if there are none.

    None means "no signal" and callers must treat it that way — never as a
    fingerprint that fails to match the vendor's known accounts.
    """
    identifiers = _identifiers(raw_details)
    if not identifiers:
        return None
    return hmac.new(secret.encode(), "|".join(identifiers).encode(), hashlib.sha256).hexdigest()
