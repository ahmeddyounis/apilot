"""API-key generation and hashing.

Keys are 256-bit random (`apk_<env>_<token_urlsafe(32)>`), so a salted slow hash adds
latency without security value and would break O(1) lookup-by-hash. SHA-256 with a
unique index is the standard API-key pattern; only the hash and a display prefix are
stored.
"""

import hashlib
import hmac
import secrets
from typing import NamedTuple

# No `admin` scope, deliberately. The only administrative operations this system has
# are key lifecycle ones, and those are CLI-only and out-of-band by design — there is
# no HTTP surface that mints, revokes or rotates a key, so there is no privileged
# endpoint to protect. A scope that gates nothing is worse than its absence: it can be
# granted, it looks like a control, and it enforces nothing. The obvious candidate for
# it, `mark_exported`, is wrong too — the accounting integration that pulls the CSV
# and marks the batch delivered is a routine consumer, and making that an admin key
# would put administrative rights on the most widely-shared credential.
SCOPES = frozenset({"ingest", "read", "review", "export"})

PREFIX_LENGTH = 12


class GeneratedKey(NamedTuple):
    full_key: str
    prefix: str
    key_hash: str


def hash_api_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode()).hexdigest()


def generate_api_key(env_tag: str) -> GeneratedKey:
    full_key = f"apk_{env_tag}_{secrets.token_urlsafe(32)}"
    return GeneratedKey(
        full_key=full_key,
        prefix=full_key[:PREFIX_LENGTH],
        key_hash=hash_api_key(full_key),
    )


def hashes_match(stored_hash: str, raw_key: str) -> bool:
    return hmac.compare_digest(stored_hash, hash_api_key(raw_key))


def parse_scopes(raw: str) -> list[str]:
    """Parse a comma-separated scope string, rejecting unknown scopes."""
    scopes = sorted({part.strip() for part in raw.split(",") if part.strip()})
    unknown = [scope for scope in scopes if scope not in SCOPES]
    if unknown:
        valid = ", ".join(sorted(SCOPES))
        raise ValueError(f"Unknown scope(s): {', '.join(unknown)}. Valid scopes: {valid}")
    if not scopes:
        raise ValueError("At least one scope is required")
    return scopes
