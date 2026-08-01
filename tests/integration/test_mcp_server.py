"""MCP round-trip: a real client session over in-memory streams calls each tool."""

import hashlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import UUID

import anyio
import pytest
from mcp import ClientSession
from mcp.shared.memory import create_client_server_memory_streams

from app.core.db import get_sessionmaker
from app.llm.fake import scenario
from app.mcp_server.server import mcp
from app.models import Invoice, InvoiceFile
from tests.integration.conftest import minimal_pdf, mint_key

pytestmark = pytest.mark.integration


@pytest.fixture
async def mcp_key(monkeypatch: pytest.MonkeyPatch) -> str:
    """stdio carries no headers, so the MCP server reads its credential from the
    environment its client launched it with."""
    raw_key, _ = await mint_key(["ingest", "read"], name="mcp-test")
    monkeypatch.setenv("APILOT_MCP_API_KEY", raw_key)
    return raw_key


@asynccontextmanager
async def mcp_client() -> AsyncIterator[ClientSession]:
    """Drive the real MCP protocol over paired in-memory streams."""
    async with create_client_server_memory_streams() as (client_streams, server_streams):
        client_read, client_write = client_streams
        server_read, server_write = server_streams
        server = mcp._lowlevel_server
        async with anyio.create_task_group() as task_group:
            task_group.start_soon(
                lambda: server.run(
                    server_read, server_write, server.create_initialization_options()
                )
            )
            async with ClientSession(client_read, client_write) as session:
                await session.initialize()
                yield session
            task_group.cancel_scope.cancel()


def payload(result: Any) -> dict[str, Any]:
    if getattr(result, "structuredContent", None):
        content = result.structuredContent
        if isinstance(content, dict):
            return content.get("result", content)
    return json.loads(result.content[0].text)


async def test_mcp_tools_round_trip(tmp_path: Path, mcp_key: str) -> None:
    invoice_path = tmp_path / "invoice.pdf"
    invoice_path.write_bytes(minimal_pdf("mcp-demo"))

    async with mcp_client() as client:
        listed = await client.list_tools()
        assert {tool.name for tool in listed.tools} == {
            "extract_invoice",
            "check_duplicates",
            "match_invoice",
        }
        extract_tool = next(t for t in listed.tools if t.name == "extract_invoice")
        assert extract_tool.description and "never guesses" in extract_tool.description

        with scenario("extract-po-clean"):
            extracted = payload(
                await client.call_tool("extract_invoice", {"file_path": str(invoice_path)})
            )
        assert extracted["status"] == "extracted"
        assert extracted["vendor_name"] == "ACME Office Supply Co"
        assert extracted["total"] == "507.60"
        assert extracted["validation"]["passed"] is True
        invoice_id = extracted["invoice_id"]

        screened = payload(await client.call_tool("check_duplicates", {"invoice_id": invoice_id}))
        assert screened["held"] is False
        assert any(flag["code"] == "F-100" for flag in screened["flags"])  # first-ever vendor

        matched = payload(await client.call_tool("match_invoice", {"invoice_id": invoice_id}))
        # No PO seeded here → honest missing_po exception, no LLM spend.
        assert matched["verdict"]["verdict"] == "exception"
        assert matched["verdict"]["discrepancies"][0]["type"] == "missing_po"
        assert [event["node"] for event in matched["trace"]] == [
            "po_lookup",
            "assemble_verdict",
        ]


async def test_mcp_reports_missing_file_honestly(tmp_path: Path, mcp_key: str) -> None:
    async with mcp_client() as client:
        result = payload(
            await client.call_tool("extract_invoice", {"file_path": str(tmp_path / "nope.pdf")})
        )
        assert "No such file" in result["error"]


async def test_mcp_tools_refuse_without_a_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """These tools reach the same pipeline the HTTP API guards with scopes and rate
    limits. Unauthenticated, they were a side door for anything able to spawn the
    process — and extract_invoice reads whatever local path it is handed."""
    monkeypatch.delenv("APILOT_MCP_API_KEY", raising=False)
    invoice_path = tmp_path / "invoice.pdf"
    invoice_path.write_bytes(minimal_pdf("mcp-unauthorised"))

    async with mcp_client() as client:
        result = payload(
            await client.call_tool("extract_invoice", {"file_path": str(invoice_path)})
        )
        assert "APILOT_MCP_API_KEY is not set" in result["error"]


async def test_mcp_tools_refuse_a_key_without_the_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw_key, _ = await mint_key(["read"], name="mcp-read-only")
    monkeypatch.setenv("APILOT_MCP_API_KEY", raw_key)
    invoice_path = tmp_path / "invoice.pdf"
    invoice_path.write_bytes(minimal_pdf("mcp-scopeless"))

    async with mcp_client() as client:
        result = payload(
            await client.call_tool("extract_invoice", {"file_path": str(invoice_path)})
        )
        assert "lacks the 'ingest' scope" in result["error"]


async def test_mcp_ingestion_is_content_addressed(tmp_path: Path, mcp_key: str) -> None:
    """A fabricated sha256 meant DUP-EXACT-FILE could never fire for an invoice
    ingested here, and pointing storage_path at the caller's file meant later stages
    broke if they moved it."""
    invoice_path = tmp_path / "invoice.pdf"
    body = minimal_pdf("mcp-hash")
    invoice_path.write_bytes(body)

    async with mcp_client() as client:
        with scenario("extract-po-clean"):
            extracted = payload(
                await client.call_tool("extract_invoice", {"file_path": str(invoice_path)})
            )
    invoice_id = extracted["invoice_id"]

    async with get_sessionmaker()() as session:
        invoice = await session.get(Invoice, UUID(invoice_id))
        assert invoice is not None
        file_row = await session.get(InvoiceFile, invoice.file_id)
        assert file_row is not None
        assert file_row.sha256 == hashlib.sha256(body).hexdigest()
        # Stored under our own roof, not the caller's path.
        assert str(invoice_path) != file_row.storage_path
        assert Path(file_row.storage_path).read_bytes() == body
