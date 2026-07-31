"""Live extraction smoke test (no database involved).

    uv run python scripts/extract_one.py path/to/invoice.pdf [--check-cache]

Requires ANTHROPIC_API_KEY in .env. --check-cache runs the identical request a
second time and prints cache-read tokens, verifying the prompt-cache prefix works
(expect cache_read_input_tokens > 0 on the second call).
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from app.core.config import get_settings
from app.llm.anthropic_client import AnthropicClient
from app.llm.pricing import compute_cost
from app.llm.types import DocumentInput
from app.schemas.extraction import arithmetic_findings, wire_to_domain
from app.schemas.extraction_wire import InvoiceExtractionWire
from app.services.extraction import EXTRACTION_SYSTEM, USER_TEXT
from app.services.storage import sniff_mime


async def extract(path: Path, check_cache: bool) -> int:
    settings = get_settings()
    if settings.resolved_llm_mode != "live":
        print("ANTHROPIC_API_KEY is not set — this smoke test needs live mode.")
        return 1

    data = path.read_bytes()
    mime = sniff_mime(data[:16])
    if mime is None:
        print(f"Unrecognized file type: {path}")
        return 1
    kind = "pdf" if mime == "application/pdf" else "image"
    doc = DocumentInput(kind=kind, data=data, media_type=mime)

    client = AnthropicClient(settings, model=settings.extraction_model)
    runs = 2 if check_cache else 1
    for run in range(1, runs + 1):
        result = await client.parse_document(
            docs=[doc],
            system=EXTRACTION_SYSTEM,
            user_text=USER_TEXT,
            output_schema=InvoiceExtractionWire,
        )
        cost = compute_cost(result.usage, result.model_id)
        print(f"\n=== run {run} · model={result.model_id} · stop={result.stop_reason} ===")
        print(
            f"tokens: in={result.usage.input_tokens} out={result.usage.output_tokens} "
            f"cache_write={result.usage.cache_creation_input_tokens} "
            f"cache_read={result.usage.cache_read_input_tokens} · cost=${cost}"
        )
        if result.parsed is None:
            print(f"NO PARSE: {result.parse_error or result.stop_reason}")
            return 1
        if run == 1:
            print(json.dumps(result.parsed.model_dump(), indent=2, default=str))
            domain, errors = wire_to_domain(result.parsed)
            if errors:
                print(f"coercion errors: {errors}")
            elif domain is not None:
                findings = arithmetic_findings(domain)
                print(f"arithmetic findings: {findings or 'none — document is consistent'}")
        elif result.usage.cache_read_input_tokens > 0:
            print("PROMPT CACHE HIT ✓")
        else:
            print("WARNING: no cache read on second run — check system-block stability")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path)
    parser.add_argument("--check-cache", action="store_true")
    args = parser.parse_args()
    sys.exit(asyncio.run(extract(args.path, args.check_cache)))


if __name__ == "__main__":
    main()
