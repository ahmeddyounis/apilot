#!/usr/bin/env bash
# APilot end-to-end demo: three invoices through the full pipeline.
#
#   make up && make demo
#
# Runs offline with the deterministic FakeLLM unless ANTHROPIC_API_KEY is set.
set -euo pipefail

API="${APILOT_API:-http://localhost:8000}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

say() { printf '\n\033[1m== %s\033[0m\n' "$1"; }
poll() {  # poll <invoice_id> -> prints final status
  local id="$1" status=""
  for _ in $(seq 1 40); do
    status=$(curl -sf -H "X-API-Key: $KEY" "$API/api/v1/invoices/$id" | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])')
    case "$status" in
      approved|exception|held|rejected|extraction_failed|exported) break ;;
    esac
    sleep 0.5
  done
  echo "$status"
}

say "Waiting for the API"
for _ in $(seq 1 60); do curl -sf "$API/health" >/dev/null && break; sleep 1; done
curl -sf "$API/health" >/dev/null || { echo "API not reachable at $API — run 'make up' first"; exit 1; }

say "Seeding vendors, purchase orders, receiving records, and a demo API key"
KEY="apk_demo_$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')"
docker compose exec -T api python -m seed.seed --api-key "$KEY" \
  || uv run python -m seed.seed --api-key "$KEY"

# Each demo invoice's bytes select its FakeLLM fixture by scenario name.
for name in demo-clean demo-overbilled demo-duplicate; do
  python3 - "$WORK/$name.pdf" "$name" <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, ".")
from evals.generator.render import render_minimal_pdf
Path(sys.argv[1]).write_bytes(render_minimal_pdf(sys.argv[2]))
PY
done

# bash 3.2 (macOS default) has no associative arrays — keep ids in plain vars.
ID_CLEAN=""; ID_OVERBILLED=""; ID_DUPLICATE=""
for name in demo-clean demo-overbilled demo-duplicate; do
  say "Uploading $name"
  response=$(curl -sf -X POST "$API/api/v1/invoices" \
    -H "X-API-Key: $KEY" -H "Idempotency-Key: demo-$name-$(date +%s)" \
    -F "file=@$WORK/$name.pdf")
  id=$(echo "$response" | python3 -c 'import json,sys; print(json.load(sys.stdin)["invoice_id"])')
  case "$name" in
    demo-clean)      ID_CLEAN="$id" ;;
    demo-overbilled) ID_OVERBILLED="$id" ;;
    demo-duplicate)  ID_DUPLICATE="$id" ;;
  esac
  echo "  invoice_id=$id  ->  status=$(poll "$id")"
done

say "Idempotent re-upload (same key + same bytes returns the same invoice)"
curl -sf -D "$WORK/headers" -X POST "$API/api/v1/invoices" \
  -H "X-API-Key: $KEY" -H "Idempotency-Key: demo-replay" -F "file=@$WORK/demo-clean.pdf" >/dev/null
curl -sf -D "$WORK/headers2" -X POST "$API/api/v1/invoices" \
  -H "X-API-Key: $KEY" -H "Idempotency-Key: demo-replay" -F "file=@$WORK/demo-clean.pdf" >/dev/null
grep -i "idempotency-replay" "$WORK/headers2" || echo "  (no replay header)"

say "Matching verdict for the over-billed invoice"
curl -sf -H "X-API-Key: $KEY" "$API/api/v1/invoices/$ID_OVERBILLED/matching" \
  | python3 -m json.tool | head -40

say "Human review queue (riskiest money first)"
curl -sf -H "X-API-Key: $KEY" "$API/api/v1/exceptions" | python3 -m json.tool | head -30

say "CSV export of approved invoices"
curl -sf -H "X-API-Key: $KEY" "$API/api/v1/exports?format=csv" | head -4

say "Analytics summary"
curl -sf -H "X-API-Key: $KEY" "$API/api/v1/analytics/summary" | python3 -m json.tool

printf '\n\033[1mDemo complete.\033[0m API key for further exploration: %s\n' "$KEY"
printf 'OpenAPI docs: %s/docs\n' "$API"
