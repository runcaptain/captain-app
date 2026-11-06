# Data model

Source: `core/captain_search/model/entities.py`. The full table and the reasoning are in [plan.md](plan.md#data-model).

Every record has `id` (ULID), `org`, `workspace`, `created_by`, `created_at` and `schema_version`.

## Write-once versions

`DatasetVersion`, `PipelineVersion`, `ShopperPopulation`, `Backtest`, `BacktestRun`,
`NotebookRevision` and `Skill` are versions. Each is a `MANIFEST.yaml` plus a `SHA256SUMS` at
`<kind-dir>/<name>/v<N>/`. Its files are content-addressed blobs at `objects/sha256/<ab>/<hash>`.

A version number is claimed by a create-only write of its MANIFEST (S3 `If-None-Match: *`,
GCS `ifGenerationMatch=0`, Azure `If-None-Match: *`, local `link()`). The loser of a race
retries with the next number.

## Mutable state (small, and compare-and-swap only)

- `aliases/<kind>/<name>/<alias>.yaml`: a pointer to a version.
- `connections/<name>.yaml`: shared connection config. A removed connection becomes a tombstone.
- `workspace.yaml`: written once at init.

## Evidence labels

Every metric carries `SIMULATED`, `LLM_GRADED`, `HUMAN_GRADED` or `REAL_TRAFFIC`. nDCG comes
only from a judgment dataset. Clicks, conversion and revenue come only from shopper logs. A run
is marked `independent: false` when its click model reads the same judgments that score it, and
`complete: false` when any arm has judged@10 under 0.9.

## Environment tiers

`demo` deploys have no gates (SOP 2026-11-02). `live` deploys need `HUMAN_GRADED` or
`REAL_TRAFFIC` evidence, or a recorded override reason.
