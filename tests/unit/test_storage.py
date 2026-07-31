import hashlib
import io
from pathlib import Path

import pytest
from fastapi import UploadFile

from app.api.errors import PayloadTooLargeError, UnsupportedMediaError
from app.services.storage import sniff_mime, store_upload

pytestmark = pytest.mark.unit

ALLOWED = ["application/pdf", "image/png", "image/jpeg", "image/webp", "image/tiff"]

PDF_BYTES = b"%PDF-1.7 fake invoice body for tests"


def upload(content: bytes, filename: str = "invoice.pdf") -> UploadFile:
    return UploadFile(io.BytesIO(content), filename=filename)


@pytest.mark.parametrize(
    ("head", "expected"),
    [
        (b"%PDF-1.4 rest", "application/pdf"),
        (b"\x89PNG\r\n\x1a\n....", "image/png"),
        (b"\xff\xd8\xff\xe0....", "image/jpeg"),
        (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "image/webp"),
        (b"II*\x00rest", "image/tiff"),
        (b"MM\x00*rest", "image/tiff"),
        (b"GIF89a....", None),
        (b"hello world", None),
    ],
)
def test_sniff_mime(head: bytes, expected: str | None) -> None:
    assert sniff_mime(head) == expected


async def test_store_upload_is_content_addressed(tmp_path: Path) -> None:
    stored = await store_upload(
        upload(PDF_BYTES), storage_dir=tmp_path, max_bytes=1024, allowed_mime=ALLOWED
    )
    expected_sha = hashlib.sha256(PDF_BYTES).hexdigest()
    assert stored.sha256 == expected_sha
    assert stored.mime == "application/pdf"
    assert stored.size_bytes == len(PDF_BYTES)
    assert Path(stored.storage_path).read_bytes() == PDF_BYTES
    assert Path(stored.storage_path).name == expected_sha

    # Re-uploading identical bytes reuses the same path without error.
    again = await store_upload(
        upload(PDF_BYTES), storage_dir=tmp_path, max_bytes=1024, allowed_mime=ALLOWED
    )
    assert again.storage_path == stored.storage_path


async def test_store_upload_enforces_size_cap(tmp_path: Path) -> None:
    with pytest.raises(PayloadTooLargeError):
        await store_upload(
            upload(PDF_BYTES), storage_dir=tmp_path, max_bytes=10, allowed_mime=ALLOWED
        )
    assert not list((tmp_path / "tmp").glob("*")), "spool file must be cleaned up"


async def test_store_upload_rejects_unknown_magic_bytes(tmp_path: Path) -> None:
    with pytest.raises(UnsupportedMediaError):
        await store_upload(
            upload(b"MZ\x90\x00 definitely-an-exe"),
            storage_dir=tmp_path,
            max_bytes=1024,
            allowed_mime=ALLOWED,
        )


async def test_store_upload_rejects_disallowed_type(tmp_path: Path) -> None:
    with pytest.raises(UnsupportedMediaError):
        await store_upload(
            upload(b"\x89PNG\r\n\x1a\n data"),
            storage_dir=tmp_path,
            max_bytes=1024,
            allowed_mime=["application/pdf"],
        )


async def test_store_upload_rejects_empty(tmp_path: Path) -> None:
    with pytest.raises(UnsupportedMediaError):
        await store_upload(upload(b""), storage_dir=tmp_path, max_bytes=1024, allowed_mime=ALLOWED)
