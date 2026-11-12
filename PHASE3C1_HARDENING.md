# Phase 3C.1 benchmark infrastructure hardening

Phase 3C.1 hardens benchmark generation, execution, verification, cancellation, and evidence handling. It does not execute or approve a production-scale benchmark and does not establish 17-million-CDR capacity.

## Implemented controls

- Fixture manifest schema 2 binds scenario, format, basis rows, expected oracle, and each input's absolute path, byte size, physical row count, and SHA-256.
- Stage runs reject manifests whose basis rows do not equal the declared stage. Explicit `--rows` overrides are restricted below Stage 1.
- Benchmark directories must be new or empty; run mode refuses prior run/result evidence.
- Deterministic isolated scenarios cover rating differences, missing/additional records, duplicate/ambiguous groups, and high-cardinality field differences in addition to identical, reordered, and mixed workloads. Fixtures rotate through every configured GPRS, Voice, and SMS matching profile.
- Oracles verify exact source rows, all classifications, ambiguous members, field and difference-type counts, result status, and exact per-currency totals and attribution components.
- Synthetic generation checks process memory, available memory, projected free disk, elapsed time, and cancellation, and records generation resource samples.
- Reconciliation monitoring now supports an elapsed-time ceiling, checks both run and temporary volumes, records I/O rates, and can continue through verification and exports.
- All Excel reports are reopened and checked for required evidence sheets. Complete Parquet exports are row-counted and all outputs are SHA-256 hashed twice.
- Benchmark result/failure and evidence manifests are atomic. Cancellation, resource abort, oracle failure, or export failure cannot publish a passing benchmark result.

## Verification status

Targeted tests cover all scenario oracles, fixture tampering, exact financial-oracle tampering, forced export failure, cancellation, input SHA-256, resource settings, and incomplete-result suppression.

Codex-side development checks completed without any workload above 1,000 rows per source:

- A 1,000-row/source mixed CSV run matched every deterministic classification, source-row, field-difference, and financial oracle and produced all 13 Excel reports and nine verified Parquet details.
- A telemetry race found by that run was corrected; subsequent samples report bounded CPU and I/O rates and label explicit row overrides as development runs rather than Stage 1.
- A 100-row/source high-cardinality run exposed one RATE value equal to the Legacy default. The fixture was corrected, and the repeated run verified 100 distinct normalized RATE differences.
- A 100-row/source mixed run rotating through all seven service profiles passed complete report and detail verification.
- Forced export failure and pre-start cancellation checks both wrote `FAILED`, exported no false passing result, and preserved their evidence directories.
- Python compilation and `pip check` pass.

Phase 3C.1 remains pending until the complete post-change suite passes in unrestricted local PowerShell. The known Codex environment can fail pytest setup with `WinError 5` while creating or cleaning pytest temporary directories; that environment error is not accepted as a regression result.

Run locally from `D:\DPM-Reconciliation`:

```powershell
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider `
  --basetemp "D:\DPM-Reconciliation\.pytest_phase3c1_post"
```

Do not execute Stage 1 until that command passes and Phase 3C.2 is explicitly approved.
