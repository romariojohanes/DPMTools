# Phase 3B safeguarded optimization

Phase 3B is implemented but is not yet complete. Completion remains gated on a
successful post-implementation full regression run from the local Windows
PowerShell environment.

## Baseline gate

The accepted pre-implementation baseline was run manually in local PowerShell:

```powershell
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider --basetemp "D:\DPM-Reconciliation\.pytest_direct_check_02"
```

Result: 80 passed, 0 failures, 0 errors, 95.02 seconds.

## Targeted change

Normalization previously updated each time field in a separate scan, then ran
separate numeric and identifier-expansion update scans. These independent
assignments are now applied in one DuckDB update per bounded normalization
chunk. Validation expressions, exact `DECIMAL(38, scale)` conversion, raw-value
retention, composite-key generation, duplicate handling, source provenance and
historical run storage are unchanged.

A focused regression covers simultaneous time, scientific-decimal and safe
scientific-identifier normalization on the same record.

## Bounded performance comparison

Both measurements used the deterministic mixed CSV scenario with 100,000
records per source, four DuckDB workers, a 4 GB DuckDB memory limit and complete
report/detail verification.

| Measurement | Before | After | Change |
| --- | ---: | ---: | ---: |
| End-to-end elapsed | 19.727 s | 19.191 s | -0.536 s (-2.7%) |
| Normalization stage | 11.715 s | 11.506 s | -0.209 s (-1.8%) |
| Peak working set | 325.8 MB | 318.2 MB | -7.6 MB (-2.3%) |

These are single local runs and show direction only; they are not a statistically
robust performance claim. Both benchmark oracles passed with 99,500 same-rating
matches, 100 rating differences, 100 records unique to each source, 200
duplicate/ambiguous groups, 200 field differences, exact zero attribution
residuals, and verified complete exports.

Evidence is retained under `data/phase3b/baseline_100000` and
`data/phase3b/optimized_100000`.

## Verification and remaining gate

- Direct targeted normalization check: passed.
- Python bytecode compilation of application, core, tests and tools: passed.
- Post-change 100,000-record/source benchmark oracle: passed.
- Sandbox pytest attempt: no tests executed; pytest produced setup errors and a
  `WinError 5` while accessing its own `--basetemp`, matching the known Codex
  environment permission issue.
- No AWS deployment or AWS-specific dependency was introduced.
- No benchmark at or above 2 million records per source was run.

Run the following in local PowerShell to satisfy the final Phase 3B gate (the
suite now contains 81 tests):

```powershell
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider --basetemp "D:\DPM-Reconciliation\.pytest_phase3b_post"
```

