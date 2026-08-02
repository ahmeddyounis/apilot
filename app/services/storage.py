"""Content-addressed upload storage.

Bytes stream to a spool file while being hashed and size-capped (Content-Length is
never trusted), the type is sniffed from magic bytes (extensions lie), and the file
lands at ``<dir>/<sha[:2]>/<sha[2:4]>/<sha256>`` — identical bytes always share one
path.
"""

import asyncio
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import aiofiles
from fastapi import UploadFile

from app.api.errors import PayloadTooLargeError, UnsupportedMediaError

_CHUNK_SIZE = 64 * 1024


@dataclass(frozen=True)
class StoredFile:
    sha256: str
    size_bytes: int
    mime: str
    storage_path: str
    original_filename: str | None


def sniff_mime(head: bytes) -> str | None:
    if head.startswith(b"%PDF-"):
        return "application/pdf"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head.startswith(b"II*\x00") or head.startswith(b"MM\x00*"):
        return "image/tiff"
    return None


async def store_bytes(
    data: bytes,
    *,
    storage_dir: Path,
    allowed_mime: list[str],
    original_filename: str | None = None,
) -> StoredFile:
    """Store an in-memory document at its content address.

    For callers that already hold the whole document — the MCP server reads a local
    path — where `store_upload`'s streaming and size cap buy nothing. The important
    part is that the file lands in the same content-addressed layout under the same
    real sha256, so exact-duplicate detection sees MCP-ingested invoices and later
    stages can still read the document if the caller moves or deletes the original.
    """
    mime = sniff_mime(data[:16])
    if mime is None or mime not in allowed_mime:
        detected = mime or "unrecognized content"
        raise UnsupportedMediaError(
            f"Unsupported file type ({detected}); allowed: {', '.join(allowed_mime)}"
        )

    sha256 = hashlib.sha256(data).hexdigest()
    final_path = storage_dir / sha256[:2] / sha256[2:4] / sha256
    await asyncio.to_thread(final_path.parent.mkdir, parents=True, exist_ok=True)
    if not await asyncio.to_thread(final_path.exists):
        async with aiofiles.open(final_path, "wb") as handle:
            await handle.write(data)

    return StoredFile(
        sha256=sha256,
        size_bytes=len(data),
        mime=mime,
        storage_path=str(final_path),
        original_filename=original_filename,
    )


async def store_upload(
    file: UploadFile,
    *,
    storage_dir: Path,
    max_bytes: int,
    allowed_mime: list[str],
) -> StoredFile:
    tmp_dir = storage_dir / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = tmp_dir / uuid4().hex

    hasher = hashlib.sha256()
    size = 0
    head = b""

    try:
        async with aiofiles.open(tmp_path, "wb") as spool:
            while chunk := await file.read(_CHUNK_SIZE):
                if not head:
                    head = chunk[:16]
                size += len(chunk)
                if size > max_bytes:
                    raise PayloadTooLargeError(f"Upload exceeds the {max_bytes}-byte limit")
                hasher.update(chunk)
                await spool.write(chunk)

        if size == 0:
            raise UnsupportedMediaError("Empty upload")

        mime = sniff_mime(head)
        if mime is None or mime not in allowed_mime:
            detected = mime or "unrecognized content"
            raise UnsupportedMediaError(
                f"Unsupported file type ({detected}); allowed: {', '.join(allowed_mime)}"
            )

        sha256 = hasher.hexdigest()
        final_path = storage_dir / sha256[:2] / sha256[2:4] / sha256
        final_path.parent.mkdir(parents=True, exist_ok=True)
        if final_path.exists():
            tmp_path.unlink(missing_ok=True)
        else:
            os.replace(tmp_path, final_path)

        return StoredFile(
            sha256=sha256,
            size_bytes=size,
            mime=mime,
            storage_path=str(final_path),
            original_filename=file.filename,
        )
    finally:
        tmp_path.unlink(missing_ok=True)
