import pytest

from app.core.security import (
    SCOPES,
    generate_api_key,
    hash_api_key,
    hashes_match,
    parse_scopes,
)

pytestmark = pytest.mark.unit


def test_generated_key_shape() -> None:
    generated = generate_api_key("dev")
    assert generated.full_key.startswith("apk_dev_")
    assert generated.prefix == generated.full_key[:12]
    assert generated.key_hash == hash_api_key(generated.full_key)
    assert len(generated.key_hash) == 64


def test_keys_are_unique() -> None:
    assert generate_api_key("dev").full_key != generate_api_key("dev").full_key


def test_hashes_match_round_trip() -> None:
    generated = generate_api_key("test")
    assert hashes_match(generated.key_hash, generated.full_key)
    assert not hashes_match(generated.key_hash, generated.full_key + "x")


def test_parse_scopes_valid() -> None:
    assert parse_scopes("read, ingest") == ["ingest", "read"]
    assert set(parse_scopes(",".join(SCOPES))) == SCOPES


def test_parse_scopes_rejects_unknown_and_empty() -> None:
    with pytest.raises(ValueError, match="Unknown scope"):
        parse_scopes("read,superuser")
    with pytest.raises(ValueError, match="At least one"):
        parse_scopes(" , ")
