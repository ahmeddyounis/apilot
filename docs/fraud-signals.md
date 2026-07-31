# Duplicate & Fraud Signals

Screening runs after validation and before matching, cheapest layer first. **No
signal ever auto-rejects an invoice.** The worst outcome is `held` with a written
explanation attached, because a wrongly-rejected invoice is a vendor relationship
problem and a wrongly-paid one is a money problem — both need a human.

## Layer 1 — exact duplicates

| Code | Trigger |
|---|---|
| `DUP-EXACT-FILE` | Byte-identical file already ingested (content-addressed sha256) |
| `DUP-EXACT-KEY` | Same normalized vendor + normalized invoice number as an existing invoice |

Normalization matters: `ACME Office Supply Co.` and `Acme Office Supply Company`
collapse to the same key, as do `INV-2026-0042`, `inv 2026 0042`, and `#0000042`.

Note the deliberate absence of a database unique constraint on
`(vendor_id, normalized_invoice_number)`. A constraint would make the second
submission fail at INSERT — an auto-reject by another name, and impossible anyway
since the vendor isn't known until extraction finishes.

## Layer 2 — near duplicates

The case that costs real money: the same invoice re-submitted under a new number.

1. Build a canonical text document from the *extraction*, not raw OCR:
   `normalized_vendor|normalized_number|date|currency|total|line descriptions+amounts`.
2. Embed it (384-dim) and search the tenant's corpus with pgvector cosine
   (HNSW index), taking candidates above `NEAR_DUP_COSINE_THRESHOLD` (0.85).
3. Score each candidate on more than the embedding:

   ```
   0.45 × cosine
 + 0.25 × fuzzy vendor-name similarity
 + 0.20 × (totals within 1%)
 + 0.10 × (dates within 7 days)
   ```

4. At or above `DUP_SUSPECT_THRESHOLD` (0.80) → `DUP-SUSPECT`, hold, with the
   matched invoice id and per-factor breakdown in the evidence.

Embeddings are pluggable (`EmbeddingProvider`): local fastembed by default so no
second API key is needed, with a deterministic hashed provider for tests and CI.

## Layer 3 — heuristic risk flags

| Code | Severity | Trigger | Why it matters |
|---|---|---|---|
| `F-100` | low | First invoice ever from this vendor | New payees are where fraud enters |
| `F-110` | **high** | Remittance bank details differ from the vendor's known details | The classic AP fraud vector — a "we've changed banks" email |
| `F-115` | medium | Bank details appear for the first time on an established vendor | Same vector, weaker evidence |
| `F-120` | low | Suspiciously round total (multiple of 100 above a floor) | Fabricated amounts cluster on round numbers |
| `F-130` | medium | Total within 5% under the auto-approve threshold | Structuring: sizing invoices to slip under review |
| `F-140` | medium | Invoice dated before its purchase order | Billing for work not yet authorized |

### Bank-detail fingerprints

`F-110` needs to compare *this* invoice's bank details against the vendor's history,
but storing bank data would make the database a target. Instead:

1. The extraction wire schema carries the verbatim remittance block in memory only.
2. Long digit runs (account/routing numbers, the numeric body of an IBAN) are
   extracted after stripping all punctuation and case, so formatting can't change
   the result.
3. Those identifiers are HMAC-SHA256'd with a server secret and only the digest is
   stored, in `vendor_payment_fingerprints`.
4. The raw text is dropped before persistence — a test asserts it never reaches the
   stored extraction JSON.

The system can therefore answer "did this vendor's bank details change?" without
ever holding the details.

## Routing

```
any DUP-* flag            → held
any high-severity flag    → held
two or more medium flags  → held
otherwise                 → annotate and continue to matching
```

A risk score (0–1) is computed from the same flags and drives the review queue's
priority sort (`risk × amount`). Low-severity flags annotate the matching verdict but
never block auto-approval — otherwise `F-100` alone would freeze a freshly-seeded
system.

## Releasing a hold

A reviewer can release a held invoice through `POST /api/v1/invoices/{id}/review`. Approving
it directly is one path; correcting a field with `rerun_matching: true` re-runs the
pipeline and clears the flags — recording the cleared codes in the append-only audit
log, so the override is a fact on the record rather than a silent state change.
