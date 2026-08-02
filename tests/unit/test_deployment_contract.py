"""The manifests encode timings the application also encodes. Drift is silent.

A worker `terminationGracePeriodSeconds` below arq's `job_completion_wait` means the
kubelet kills jobs arq was still finishing — on every rollout, with no error, the
invoices landing in the stale-job cron for a 15-minute rescue. Nothing else would
catch that, so it is asserted here.
"""

from pathlib import Path

import pytest
import yaml

from app.core.config import Settings
from app.worker import WorkerSettings

pytestmark = pytest.mark.unit

_BASE = Path(__file__).resolve().parents[2] / "deploy" / "k8s" / "base"


def manifest(name: str) -> dict:
    return yaml.safe_load((_BASE / name).read_text())


def test_worker_grace_period_outlasts_arq_job_completion_wait() -> None:
    spec = manifest("worker-deployment.yaml")["spec"]["template"]["spec"]
    assert spec["terminationGracePeriodSeconds"] > WorkerSettings.job_completion_wait


def test_job_timeout_covers_both_stage_timeouts() -> None:
    """A job that the stage timeouts allow must not be killed by arq mid-stage."""
    settings = Settings(_env_file=None)
    stages = settings.stage_timeout_extraction_s + settings.stage_timeout_matching_s
    assert WorkerSettings.job_timeout > stages


def test_db_pool_covers_worker_concurrency() -> None:
    """Each concurrent job may hold a session. A job that blocks on checkout and then
    times out is dropped by arq rather than retried."""
    config = manifest("configmap.yaml")["data"]
    max_jobs = int(config["APILOT_WORKER_MAX_JOBS"])
    capacity = int(config["APILOT_DB_POOL_SIZE"]) + int(config["APILOT_DB_MAX_OVERFLOW"])
    assert capacity > max_jobs


def test_probes_use_the_endpoints_they_are_meant_to() -> None:
    """Liveness must not do I/O, or a database blip restarts healthy containers;
    readiness must, or an unusable pod stays in rotation."""
    container = manifest("api-deployment.yaml")["spec"]["template"]["spec"]["containers"][0]
    assert container["livenessProbe"]["httpGet"]["path"] == "/health"
    assert container["readinessProbe"]["httpGet"]["path"] == "/health/ready"


def test_prod_config_forces_the_startup_guard() -> None:
    """APILOT_ENV=prod is what makes the app refuse the published fingerprint secret."""
    assert manifest("configmap.yaml")["data"]["APILOT_ENV"] == "prod"


def test_containers_run_unprivileged_and_read_only() -> None:
    for name in ("api-deployment.yaml", "worker-deployment.yaml"):
        spec = manifest(name)["spec"]["template"]["spec"]
        assert spec["securityContext"]["runAsNonRoot"] is True
        assert spec["automountServiceAccountToken"] is False
        container = spec["containers"][0]
        assert container["securityContext"]["readOnlyRootFilesystem"] is True
        assert container["securityContext"]["allowPrivilegeEscalation"] is False
