# Deployment

The project runs as three containers plus managed data stores. Nothing here is
platform-specific beyond the examples.

## What you need

| Component | Notes |
|---|---|
| API container | `uvicorn app.main:app`, port 8000 |
| Worker container | Same image, `arq app.worker.WorkerSettings` |
| PostgreSQL 17 + pgvector | The `vector` extension must be installable |
| Redis | Rate limiting, job queue |
| Persistent volume | Uploaded invoice files (`APILOT_STORAGE_DIR`) |

Both containers come from the same image; only the command differs.

## Environment

Copy `.env.example` and set at minimum:

```
APILOT_DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/apilot
APILOT_REDIS_URL=redis://host:6379/0
APILOT_STORAGE_DIR=/data/uploads
APILOT_LOG_FORMAT=json
APILOT_FINGERPRINT_SECRET=<32+ random bytes>   # rotating this invalidates fraud history
ANTHROPIC_API_KEY=<key>                         # omit to run on the FakeLLM
```

`APILOT_FINGERPRINT_SECRET` keys the bank-detail HMACs. Treat it like a signing key:
generate it once, store it in the platform's secret manager, and don't rotate it
casually — existing vendor fingerprints stop matching if you do.

**With `APILOT_ENV=prod` the app refuses to start** on the default fingerprint
secret (the same literal `.env.example` ships, which is public), on one shorter than
32 bytes, or on the local dev database URL. It reports every problem at once and
exits. That is deliberate: a signing key silently falling back to a published default
leaves no runtime symptom — the pipeline keeps working and the fraud check keeps
reporting, it just reports something an attacker can compute.

Generate one with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

## Migrations

Run `alembic upgrade head` as a release/pre-deploy step, not at container start, so
two starting replicas can't race. The migration creates the `vector` extension, which
requires a database user with sufficient privileges (most managed Postgres offerings
allow this for pgvector; some need the extension enabled in the console first).

## Kubernetes

Committed manifests live in [`deploy/k8s`](../deploy/k8s) — a Kustomize base with
`kind` and `prod` overlays, validated in CI with `kubeconform`. That README also
states plainly which parts are production-grade and which are demonstration.

```bash
kubectl apply -k deploy/k8s/overlays/prod
kubectl wait --for=condition=complete job/apilot-migrate -n apilot --timeout=300s
kubectl rollout status deploy/apilot-api -n apilot
```

The PaaS recipes below remain valid for a single-container deployment.

## Fly.io sketch

```bash
fly launch --no-deploy                     # writes fly.toml from the Dockerfile
fly postgres create --name apilot-db       # then: fly postgres attach apilot-db
fly redis create
fly secrets set ANTHROPIC_API_KEY=... APILOT_FINGERPRINT_SECRET=...
fly volumes create apilot_uploads --size 1
fly deploy
fly ssh console -C "alembic upgrade head"
fly ssh console -C "python -m seed.seed"   # demo data, optional
```

Add the worker as a second process in `fly.toml`:

```toml
[processes]
  api = "uvicorn app.main:app --host 0.0.0.0 --port 8000"
  worker = "arq app.worker.WorkerSettings"
```

## Render sketch

- **Web Service** from the Dockerfile; health check path `/health`.
- **Background Worker** from the same repo, command
  `arq app.worker.WorkerSettings`.
- **PostgreSQL** and **Key Value (Redis)** add-ons; wire their URLs into the env.
- Add a pre-deploy command `alembic upgrade head`.
- Attach a disk for `APILOT_STORAGE_DIR` (or switch storage to object storage — see
  below).

## Before you call it production

The build makes some choices that are right for a portfolio deployment and wrong for
a real AP department:

- **File storage is a local volume.** Multi-replica deployments need object storage
  (S3/GCS) behind the same `storage.py` interface.
- **`/metrics` is unauthenticated**, on both the API (8000) and the worker's own
  exporter (9110). Keep them behind network policy; the manifests ship a
  NetworkPolicy for the worker, but note that kind's default CNI does not enforce
  one, so CI passing is not evidence it works.
- **Rate limits are per-API-key in a single Redis.** Fine at this scale; a global
  limiter needs a shared window strategy.
- **No backup/restore runbook.** The audit log is the system of record for financial
  decisions — it needs point-in-time recovery. This is the largest remaining gap.
- **No encryption at rest for invoice documents**, no retention or deletion path, and
  no read-access auditing — writes are audited, reads are not.
- **The demo seed installs a wide-scope API key.** Don't run `seed.py` against a
  production database.
