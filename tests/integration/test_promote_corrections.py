"""The product's central loop: a reviewer correction becomes an eval case."""

import json
from pathlib import Path

import httpx
import pytest

from evals.promote_corrections import promote
from tests.integration.conftest import minimal_pdf
from tests.integration.test_extraction_flow import run_scenario

pytestmark = pytest.mark.integration


async def test_correction_becomes_labeled_eval_case(
    client: httpx.AsyncClient,
    full_access_key: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invoice = await run_scenario(
        client, full_access_key, "extract-corrupt-total", minimal_pdf("promote-me")
    )
    response = await client.post(
        f"/api/v1/invoices/{invoice.id}/review",
        headers={"X-API-Key": full_access_key},
        json={
            "action": "correct",
            "corrections": [{"field_path": "total", "new_value": "507.60"}],
        },
    )
    assert response.status_code == 200

    # chdir only now: the pipeline above resolves fixture dirs relative to cwd.
    monkeypatch.chdir(tmp_path)
    promoted = await promote("v1", since=None, dry_run=False)
    assert promoted == 1

    case_dir = tmp_path / "evals" / "dataset" / "v1" / "cases" / f"corrected_{str(invoice.id)[:8]}"
    truth = json.loads((case_dir / "truth.json").read_text())
    meta = json.loads((case_dir / "meta.json").read_text())

    # Ground truth is the human-verified value, not what the model extracted.
    assert truth["total"] == "507.60"
    assert meta["provenance"] == "reviewer_correction"
    assert meta["corrected_fields"] == ["total"]
    assert meta["corrections"][0]["extracted"] == "600.00"
    assert meta["corrections"][0]["corrected"] == "507.60"
    assert (case_dir / "invoice.pdf").is_file()  # case is self-contained

    manifest = json.loads((tmp_path / "evals/dataset/v1/manifest.json").read_text())
    assert manifest["promoted_corrections"] == 1
