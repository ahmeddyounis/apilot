# APilot — Latest Eval Report

One row per configuration (latest run of each). Fixture mode replays
recorded model outputs through the real pipeline; live mode calls the API.

| Configuration | Mode | Totals F1 | Line items F1 | Doc exact match | Seeded errors caught | Matching acc. | Dedup recall |
|---|---|---|---|---|---|---|---|
| naive | fixture | 0.9524 | 0.9174 | 0.9091 | 0.0 | — | — |
| schema_retry | fixture | 1.0 | 1.0 | 1.0 | 0.0 | — | — |
| validation | fixture | 1.0 | 1.0 | 1.0 | 1.0 | 1.0 | 1.0 |

_Refreshed 2026-08-01T18:50:02+00:00 — see `evals/reports/` for full per-run JSON, including per-field F1._
