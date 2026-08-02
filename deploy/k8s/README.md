# Kubernetes manifests

Kustomize base plus two overlays. Kustomize rather than Helm because Helm's value is
distribution to strangers — a `values.yaml` as public API, versioned chart releases —
and this is single-tenant with one operator who owns the repo. Opening
`base/worker-deployment.yaml` should show a Deployment, not a template you have to
render to understand.

```bash
kubectl apply -k deploy/k8s/overlays/kind    # laptop / CI
kubectl apply -k deploy/k8s/overlays/prod    # managed Postgres + Redis
```

Migrations run as a Job, never at container start, so two starting replicas cannot
race each other:

```bash
kubectl apply -k deploy/k8s/overlays/prod
kubectl wait --for=condition=complete job/apilot-migrate -n apilot --timeout=300s
kubectl rollout status deploy/apilot-api -n apilot
```

## The numbers that have to move together

| Setting | Value | Tied to |
|---|---|---|
| `job_timeout` (arq) | 420s | Both stage timeouts (180 + 180) plus overhead |
| `job_completion_wait` | 90s | Worker `terminationGracePeriodSeconds` |
| Worker `terminationGracePeriodSeconds` | 120s | Must exceed `job_completion_wait` |
| API `--timeout-graceful-shutdown` | 20s | API `terminationGracePeriodSeconds` (30s) minus `preStop` (5s) |
| `APILOT_WORKER_MAX_JOBS` | 8 | `APILOT_DB_POOL_SIZE` (10) + overflow (10) per process |

A grace period shorter than `job_completion_wait` means the kubelet kills jobs arq
was still finishing — on *every* rollout, silently, with the invoices landing in the
stale-job cron for a 15-minute rescue. `max_jobs` above what the pool can serve means
jobs block on checkout and then time out, and arq drops a job that raises rather than
retrying it.

## Probes

Liveness is `/health`, which does no I/O by design — a database blip must not restart
a container that is itself healthy. Readiness is `/health/ready`, which checks
Postgres and Redis under a 2s timeout and returns 503 when degraded, so an unusable
pod leaves the load balancer without being killed. The worker has no HTTP server, so
its liveness is `arq --check`, which reads the health key arq refreshes every 30s —
a real signal, unlike the metrics exporter, which would answer even if the event loop
were wedged.

## Genuinely production-grade

- The probe split and the reasoning behind it
- Graceful-shutdown reconciliation across arq, uvicorn and the kubelet
- Migration Job ordered ahead of rollout by an explicit `kubectl wait`, with a
  separate privileged database URL for `CREATE EXTENSION vector`
- SecurityContext: non-root with a fixed UID, `readOnlyRootFilesystem` with the two
  writable mounts the process actually needs, all capabilities dropped, no service
  account token
- ConfigMap/Secret split, so a cluster-wide read does not leak a signing key
- `APILOT_ENV=prod` in the ConfigMap, which makes the app refuse to start on the
  published fingerprint-secret default rather than quietly HMACing bank details
  with a value anyone can read

## Demonstration only — do not mistake this for a deployment

- **In-cluster Postgres and Redis in the kind overlay.** Single replica, `emptyDir`
  storage, no backups, no point-in-time recovery, no failover, password in git.
- **The RWX claim in `base` is untested.** It is correct in principle — the API
  writes the document and the worker reads it back, so they must share a filesystem
  — but CI runs single-node RWO via the kind overlay, so nothing here proves RWX
  behaves. The real fix is object storage behind a seam in
  `app/services/storage.py`; it is named as a follow-up rather than half-built.
- **The NetworkPolicy is schema-valid and unenforced in CI.** kind's default CNI
  ignores NetworkPolicy, so a green pipeline says nothing about whether the policy
  works.
- **Resource requests and limits are reasoned, not load-tested.** They come from what
  the components do — the worker loads an ONNX model and rasterises PDFs — not from
  measurement under load.
- **No HPA, no ServiceMonitor, no PodDisruptionBudget, no backup/restore runbook.**
  The last one matters most: the audit log is the system of record for financial
  decisions and needs point-in-time recovery before this is real.

## Verified in CI

`kustomize build` for every overlay, piped through `kubeconform -strict` against the
Kubernetes 1.31 schemas. Separately, the built image is started and asked for
`/health` — it had never been *run* in CI before, only compiled, so a runtime-only
failure such as a missing dependency in the `embeddings` group would have shipped
green.
