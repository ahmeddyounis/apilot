# Architecture Decisions

Short records of the choices that shaped the system, and what each one costs.

## 1. LLMs extract, code decides

The vision model's only job is transcription. Every consequential judgment —
whether the arithmetic holds, whether an invoice matches its PO, whether it may be
approved — is made by deterministic code that a reviewer can read and a test can pin.

The clearest expression of this is the failure taxonomy in extraction:

| Failure | Handling | Why |
|---|---|---|
| Model refusal | Terminal `extraction_failed`, no retry | Retrying a refusal is wasted spend |
| Schema / coercion failure | Retry with feedback (max 2), then terminal | A model-output defect; guessing around it would invent data |
| **Arithmetic inconsistency** | Retry with feedback, then **accept as extracted** | The document itself may be wrong — transcribing it faithfully is correct; deciding what to do about it is the validation engine's job |

That third row is the whole thesis. A faithfully-extracted invoice whose printed
total doesn't equal subtotal + tax is *not* an extraction failure; it's an invoice
that needs a human. There's an integration test asserting exactly that path.

## 2. Money is Decimal, everywhere, always

`NUMERIC` in Postgres, `Decimal` in Python, **strings on both wires**. JSON numbers
are IEEE floats; a float cent error in an AP system is a defect, not a rounding
detail. So the LLM is instructed to emit amounts as digit strings, and API responses
serialize Decimals as strings.

Equality is currency-aware: tolerance is one unit in the currency's last decimal
place (JPY 0dp, most currencies 2dp, BHD 3dp), shared by the extraction checks and
the validation rules so both layers agree on what "equal" means.

## 3. One choke point for status changes

Every mutation of `invoices.status` goes through `state_machine.transition()`, which
validates the edge against an explicit map, stamps lifecycle timestamps, writes the
audit row **in the same transaction**, and increments a Prometheus counter. Nothing
else may assign that column.

A unit test walks the transition graph and asserts every status can reach a terminal
state — an invoice can never get stuck.

## 4. The agent is a graph, not a loop

Matching is a LangGraph `StateGraph` where PO lookup, line alignment, and all
discrepancy arithmetic are plain code. The single LLM node runs only when the
deterministic pass leaves genuine ambiguity (a price drift to explain, unmatched
lines on both sides), is bounded to four tool turns with read-only tools, and its
proposals are re-validated by code before they affect anything.

Consequences worth stating plainly:

- A clean invoice costs **zero** LLM tokens in matching — verified by a test that
  asserts no `llm_calls` rows for the clean path.
- A missing PO short-circuits to an exception without a model call. Spending tokens
  to *guess* at a purchase order inverts the product's entire honesty stance.
- The model cannot approve anything. `auto_approve_eligible` is computed from
  deterministic checks plus the configured threshold.

Every node emits a `NodeEvent` (sequence, duration, input/output digests) into a
replayable trace persisted alongside the verdict.

## 5. Idempotency because the product demands it

A system whose purpose is catching duplicate invoices must not create them. The
upload endpoint requires an `Idempotency-Key`; the same key with the same bytes
replays the original response (with an `Idempotency-Replay: true` header), and the
same key with different bytes is a 409. Concurrent requests race on a primary-key
insert and the loser adopts the winner's result.

## 6. Append-only audit, enforced by the database

`audit_log` has a `BEFORE UPDATE OR DELETE` row trigger and a `BEFORE TRUNCATE`
statement trigger, both raising. Application code cannot rewrite history even by
accident; an integration test proves the trigger fires. Financial workflows demand
this, and enforcement at the storage layer is the only kind that survives a bug.

## 7. Costs are measured, not estimated

Every LLM API attempt — including retries — writes an `llm_calls` row with all four
token counts (input, output, cache write, cache read) and a Decimal USD cost from a
config-driven pricing table. "Cost per invoice" is a headline metric of this product,
so it is computed from what actually happened rather than a per-call estimate.

## 8. Fakes are first-class, not test doubles

`FakeLLM` and `FakeEmbeddings` are deterministic, fixture-driven implementations of
the same protocols as the real clients, and the entire test suite plus the CI eval
gate runs on them. The system boots and processes invoices end-to-end with an empty
environment — no secrets in CI, ever. Missing fixtures fail loudly rather than
returning plausible nonsense.

## 9. Compose ports are remapped on purpose

Postgres publishes on **5433** and Redis on **6380** because 5432/6379 were already
taken on the development machine. Container-internal URLs use service DNS and default
ports; only the host mapping shifts. Documented because a surprising port is exactly
the kind of thing that wastes an hour.

## 10. Single-tenant by design, and what that means

There is no organization, tenant, or owner entity anywhere in the schema. An API key
is a credential, not a customer: `AuthContext` carries an id, a name and scopes, and
no query in the system filters on it. **Any valid key with `read` can fetch any
invoice in the database**, and `GET /exceptions` will enumerate them for it.

That is a deployment boundary, not an oversight. The unit of isolation is the
deployment: one database, one storage volume, one set of keys, one customer. Scopes
divide what a key may *do*; they were never meant to divide what it may *see*.

Stated plainly because the alternative is worse. A `tenant_id` column that most
queries filter on and a few forget is a system that looks isolated and leaks, and the
forgetting happens on whichever endpoint was added last. If this ever needs real
multi-tenancy, the honest version is a schema change and a filter on every read —
not a middleware that hopes.

The seam, if that day comes: add `tenant_id` to `api_keys`, carry it on
`AuthContext` (`app/api/deps.py`), and constrain the six query sites that currently
select invoices without an owner predicate — `app/services/invoices.py`
(`get_invoice_detail`), `app/api/v1/exceptions.py`, `app/api/v1/matching.py`,
`app/services/review.py` (`load_for_review`), `app/services/exports.py`, and
`app/services/analytics.py`. Screening and matching read across invoices by
construction (duplicate detection compares an invoice to its corpus), so they would
need the predicate threaded through rather than bolted on — which is the real cost,
and the reason this is a decision rather than a to-do.

**Compliance boundaries that follow from the same staging choice**, currently
absent and documented as such rather than half-built: invoice documents are stored
unencrypted on the volume; there is no retention or deletion path; read access is
not audited (only writes are); and there is no backup/restore runbook for the audit
log, which is the system of record for financial decisions.

## Deliberate non-goals (v1)

Payment execution, ERP write-back connectors, OCR model training, and multi-currency
consolidation are out of scope. The system prepares approvals; it never moves money.
