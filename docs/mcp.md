# MCP Server

APilot exposes its pipeline as [Model Context Protocol](https://modelcontextprotocol.io)
tools, so any MCP client (Claude Desktop, Claude Code, your own agent) can extract,
screen, and match invoices natively. The tools call the same service layer as the
HTTP API — there is no second implementation.

## Prerequisites

```bash
docker compose up -d db          # Postgres must be reachable
make migrate
```

Without `ANTHROPIC_API_KEY` in `.env` the server runs on the deterministic FakeLLM,
which is useful for wiring up a client but will only return data for documents that
have a matching fixture.

## Run it

```bash
uv run python -m app.mcp_server
```

The server speaks stdio, so clients launch it themselves rather than connecting to a
port.

## Client configuration

Add to `.mcp.json` in a project (Claude Code) or `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "apilot": {
      "command": "uv",
      "args": ["run", "python", "-m", "app.mcp_server"],
      "cwd": "/absolute/path/to/apilot"
    }
  }
}
```

## Tools

### `extract_invoice(file_path: str)`

Reads a local PDF or image, extracts it into the strict schema, and runs the
deterministic validation rules. Returns the extracted fields, per-field confidence,
validation findings, the LLM cost for the call, and an `invoice_id` for the other
tools.

Extraction failures are reported as failures — the tool never returns a guessed
value. A refusal, an unreadable document, or output that fails the schema after
retries all come back as `{"status": "extraction_failed", "error_detail": ...}`.

```json
{
  "invoice_id": "0f6a…",
  "status": "extracted",
  "vendor_name": "ACME Office Supply Co",
  "invoice_number": "INV-2026-1001",
  "total": "507.60",
  "line_items": [{"line_no": 1, "description": "Widget A-200", "quantity": "10", ...}],
  "validation": {"passed": true, "findings": [{"rule_id": "V-012", "severity": "flag", ...}]},
  "llm_cost_usd": "0.026950"
}
```

### `check_duplicates(invoice_id: str)`

Screens an extracted invoice: exact file/key duplicates, embedding near-duplicates,
and the heuristic fraud flags (new vendor, changed bank details, round numbers,
just-under-threshold amounts, invoice-before-PO). Every flag carries a written
explanation and evidence. Flags never auto-reject — they hold the invoice.

### `match_invoice(invoice_id: str)`

Runs 3-way matching against purchase orders and receiving records. Returns the
structured verdict (per-check results, quantified discrepancies with monetary
impact, a human-readable summary) plus the replayable agent trace. The verdict is
assembled by deterministic code; the model only investigates ambiguity.

## Smoke test

```bash
uv run pytest tests/integration/test_mcp_server.py
```

That test starts the server over in-memory streams, runs a real `ClientSession`
handshake, lists the tools, and calls all three end to end against a throwaway
Postgres container.

## Security note

The server runs locally over stdio with the same database credentials as the API.
It reads files from paths the client supplies — run it only against invoice
directories you trust, exactly as you would any local tool.
