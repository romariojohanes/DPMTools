# Phase 3A production-scale readiness

Phase 3A adds the controls and repeatable evidence needed before claiming capacity at 17 million records per source. It does not itself certify that scale. No production CDRs are uploaded or used by the benchmark generator.

## Implemented controls

- Every input is SHA-256 hashed before and after ingestion. Both digests, verification status and hashing time are retained in `run.json` and Excel source metadata.
- DuckDB defaults to a 4 GB memory budget and four workers. Wide field comparison, attribution, saved-run queries and complete exports continue to use one worker.
- Normalization is performed in deterministic 250,000-row chunks. The ingestion ordinal is only an internal pointer; composite-key behavior and provenance remain unchanged.
- Each run records process memory, CPU, process I/O, temporary storage, run storage, available memory and free disk in `resource_samples.jsonl`, with a summary in `run.json`.
- A run is interrupted and marked `ABORTED_RESOURCE_LIMIT` if process working set exceeds 14 GB, available memory falls below 4 GB, or free disk falls below 50 GB. User cancellation produces `CANCELLED`. Neither status publishes a reconciliation result.
- DuckDB temporary storage is capped per run. The application default is 20 GB; staged benchmark caps are 20/35/55/75 GB.
- Complete detail exports run in the Streamlit background, use one DuckDB worker, write atomically, and leave the interface responsive.

The DuckDB memory limit is not a whole-process limit. Python, Streamlit, openpyxl, native allocations and filesystem cache remain outside it.

## Benchmark scenarios

`tools.phase3a_benchmark` supports deterministic `identical`, `reordered`, `rating_difference`, `missing_additional`, `duplicate_ambiguous`, `mixed`, and `high_cardinality_mismatch` datasets in CSV, XLSX, or cross-format form. XLSX is partitioned into multiple files. The mixed scenario includes rating changes, Legacy-only and SaaS-only records, duplicate groups, field differences, and exact financial attribution. The high-cardinality scenario gives every SaaS row a distinct RATE difference.

Generation runs in a separate process so its peak memory does not contaminate processing measurements. Generation has its own memory/disk/time/cancellation guard. The versioned fixture manifest binds every input by absolute path, byte size, row count, and SHA-256. A successful run verifies source and classification counts, ambiguous members, exact field/type differences, exact per-currency financial totals and attribution, all Excel reports, complete Parquet row counts, and repeated SHA-256 checks.

Benchmark directories must be new or empty. Run mode refuses directories containing prior results or runs, so stale evidence cannot be mistaken for a new success. Processing and all verification/export work remain resource monitored. Result, failure, and evidence manifests are written atomically; a failed or cancelled run cannot publish `PASSED`.

Development smoke test only:

```powershell
.\.venv\Scripts\python.exe -m tools.phase3a_benchmark `
  --rows 1000 --scenario mixed --format csv `
  --directory data\phase3a\smoke
```

Approved staged commands use `--stage 1` through `--stage 4`; do not substitute `--rows` when collecting certification evidence. Run each scenario in a separate directory so evidence is never overwritten.

```powershell
.\.venv\Scripts\python.exe -m tools.phase3a_benchmark `
  --stage 1 --scenario identical --format csv `
  --directory data\phase3a\stage1_identical_run1
```

Stages contain 2M, 5M, 10M and 17M records per source. Do not advance until two consecutive mandatory runs pass and the next-stage storage forecast still leaves at least 50 GB free on D:.

## Go/no-go gates

- All automated regressions pass before each stage.
- SHA-256 verification, expected classifications, output counts and exact attribution all pass.
- Peak working set remains at or below 14 GB and available memory remains at or above 4 GB.
- Temporary storage remains below its stage cap and drive D: never falls below 50 GB free.
- No OOM, interrupted output, corrupt database, unexplained residual or incomplete export occurs.
- CSV elapsed-time ceilings are 30, 60, 120 and 240 minutes for Stages 1-4.

Stage 4 is blocked if Stage 3 projects an active footprint above the available capacity after reserving 50 GB. Historical runs and reports are never deleted automatically. Capacity claims must name the exact format, scenario, settings and outputs that passed twice.

## Known boundaries

- CSV is the primary Stage 4 certification format. XLSX remains a multi-file compatibility gate and is advanced only while time and storage margins remain safe.
- Excel detail sheets are bounded to 50,000 rows. Complete evidence is CSV or Parquet, with exported counts recorded separately.
- Worst-case full audit can produce up to 24 difference rows per matched pair. It must not be extrapolated from identical-data compression ratios.
- Deep Streamlit pages still use database sorting and offset navigation. Use exact filters and complete disk exports for very large investigations; keyset navigation remains a future optimization if staged measurements show it is required.
