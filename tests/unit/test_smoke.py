import pytest

from app import __version__

pytestmark = pytest.mark.unit


def test_version() -> None:
    assert __version__ == "0.1.0"
