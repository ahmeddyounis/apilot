"""MCP round-trip: a real client session over in-memory streams calls each tool."""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp import ClientSession
from mcp.shared.memory import create_client_server_memory_streams

from app.llm.fake import scenario
from app.mcp_server.server import mcp
from tests.integration.conftest import minimal_pdf

pytestmark = pytest.mark.integration


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


async def test_mcp_tools_round_trip(tmp_path: Path) -> None:
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


async def test_mcp_reports_missing_file_honestly(tmp_path: Path) -> None:
    async with mcp_client() as client:
        result = payload(
            await client.call_tool("extract_invoice", {"file_path": str(tmp_path / "nope.pdf")})
        )
        assert "No such file" in result["error"]
