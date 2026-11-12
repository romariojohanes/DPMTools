# Phase 2 validation and performance evidence

All CDR processing and verification ran locally. No subscriber identifiers are included in this document or benchmark summaries. Existing source workbooks and historical run databases remain unchanged.

## Actual validation failure

Saved run: `data/runs/20261026T143145Z_c7d23864`. It contained 1,131,938 issues, not a generic schema failure. Both environments had exactly the same frequencies:

| Field | Issue code | Legacy | SaaS |
|---|---|---:|---:|
| IMSI | UNSAFE_EXCEL_CELL | 182,172 | 182,172 |
| ANUM | UNSAFE_EXCEL_CELL | 182,072 | 182,072 |
| GPRS_CHARGING_ID | UNSAFE_EXCEL_CELL | 181,930 | 181,930 |
| BNUM | UNSAFE_EXCEL_CELL | 100 | 100 |
| TAX_AMOUNT_TAP | INVALID_DECIMAL | 9,847 | 9,847 |
| FINAL_TAX_AMOUNT | INVALID_DECIMAL | 9,839 | 9,839 |
| RATED_AMOUNT_TAP | INVALID_DECIMAL | 9 | 9 |

The ingestion code rejected every non-string Excel identifier. All saved affected identifiers were nonnegative integral strings of at most 15 digits. These are now accepted as exact strings. Numeric Excel values beyond 15 digits, negative/nonintegral values, booleans, and unsafe scientific identifier forms remain blocking issues. Already-discarded source digits or leading zeros cannot be reconstructed.

The financial failures were precision rejections at the selected scale of 12: saved rejected amounts had up to 20 fractional digits. The previous openpyxl conversion also passed numbers through binary float representations. Native numeric tokens now come directly from worksheet XML; scientific notation expands through string operations. Raw tokens remain intact even when comma locale is selected. Precision is configurable from 0 to 30 within DECIMAL's 38 total digits; the real-data verification used scale 24. No financial rounding, error suppression, row skipping, or validation override was used. Excess precision still blocks a run and the UI reports the observed fractional-digit requirement.

Real workbooks were available, each containing 182,172 records. Both store the native Excel date January 4, 2026. This is retained; a stored Excel calendar date cannot be reinterpreted as April 1. Text `04/01/2026` uses the default MM/DD/YYYY and means April 1.

Machine-readable diagnosis: `data/validation_root_cause.json`.

## Implementation and verification order

The original suite passed 42 tests before edits. The ingestion and Phase 1 regression gate then passed 45 tests before matching implementation began. A real-data Phase 1 run completed with zero issues, at `data/phase2_ingestion_verification/20261027T121212Z_cf30faf8`, in 137.374 seconds. Its aggregate result was MISMATCH.

The first complete Phase 2 run, `data/runs/20261027T121718Z_ecb87627`, identified 303 unique GPRS rating differences and nine duplicate GPRS groups (18 records on each side). Its per-currency attribution residual was exactly zero. Direction-specific Voice/SMS profiles were subsequently added for the service values found in these inputs. Final verification results follow below.

## Performance method and limits

`python -m tools.benchmark --rows N --memory 512MB --directory ...` generates unique synthetic session keys, then measures ingestion, normalization, aggregation, matching, field comparison, attribution, Parquet output, a bounded Excel executive summary, and a complete joined-detail Parquet export. Data generation and UI rendering are outside elapsed time. Peak process working set includes generation and native DuckDB allocations; the engine memory limit is not an RSS cap.

Each source is read sequentially. CSV uses a bounded provenance pass plus DuckDB native parsing. XLSX uses streaming openpyxl, a parallel numeric-token XML cursor, disk-spooled rows, and a disk-backed shared-string index with bounded caches. Source ordinals are pointers only; composite keys determine matching. Disk-backed intermediate tables, bounded cursor exports, and SQL pagination avoid full Pandas datasets.

The initial million-row/source test exposed an export OOM at 512 MB with four query workers, despite successful reconciliation. A one-worker retry exported all 1,000,000 detail rows in 4.455 seconds. A repeat also exposed a wide field-comparison join OOM with four workers. Wide comparison/attribution stages and saved-run investigation/export queries now use one worker; configured parallelism remains available for ingestion, aggregation and narrow key matching. Failed benchmark evidence is retained in `data/phase2_benchmark/csv_1000000/*regression.json`.

The historical Phase 1 benchmark was 11.163 seconds for 100,000 rows/source, at 16 GB, excluding CDR matching and reporting. It is not a like-for-like full Phase 2 speed baseline. Intermediate Phase 2 measurements at 100,000 rows/source were 11.773 and 12.732 seconds; consolidation alone did not establish a speed gain. No improvement factor is claimed from these timings. The confirmed improvements remove blanket numeric-identifier rejection, Python per-row DuckDB inserts, an inefficient matching join guard, and unbounded shared-string loading.

**17 million records per source have not been benchmarked.** Capacity, SSD temporary-space requirements, worst-case duplicates, and high-cardinality full-field mismatches still require representative production-scale testing. No 17-million-record throughput or memory guarantee is claimed.

## Final real-data results

Run: `20261027T123553Z_1745b3f6`, available in the normal application run history. Both sources ingested all 182,172 rows with **zero validation issues**. Exact native tokens require 23 fractional digits; scale 24 was selected. The full 24-field audit found only 303 RATED_AMOUNT_TAP normalized-value differences.

| Classification | Unique pairs / groups | Legacy records | SaaS records |
|---|---:|---:|---:|
| GPRS / DUPLICATE_OR_AMBIGUOUS | 9 | 18 | 18 |
| GPRS / MATCHED_DIFFERENT_RATING | 303 | 303 | 303 |
| GPRS / MATCHED_SAME_RATING | 181,609 | 181,609 | 181,609 |
| SMS-MO / MATCHED_SAME_RATING | 97 | 97 | 97 |
| VOICE-MO / MATCHED_SAME_RATING | 45 | 45 | 45 |
| VOICE-MT / MATCHED_SAME_RATING | 100 | 100 | 100 |

There are no Legacy-only or SaaS-only records. The nine GPRS duplicate groups remain unresolved (18 records per environment), even though their net rated difference is zero. No duplicate members were paired.

Exact USD total variance: **605.999999999999969700303000**. Matched-rating differences explain all of it; SaaS-only, negative Legacy-only, unresolved duplicate variance, and unexplained residual are exactly zero.

Processing took 117.917 seconds, with matching 1.016 seconds and field comparison 0.857 seconds. The regression suite was running concurrently, so this is verification timing, not an isolated performance comparison. All 13 Excel reports and three complete CSV detail extracts were generated locally in the run's reports directory. Reporting took 1.312 seconds.

Machine-readable results: `data/phase2_real_verification.json`. Exact CDR source references and both raw and normalized values are in `reports/rating_differences.csv` and `reports/field_differences.csv`.

## Final automated verification

75 tests passed, including all format combinations, reordered sessions, missing records, duplicate/incomplete keys, Voice/SMS directions, scientific notation, exact XML financial digits, stale XLSX dimensions, shared strings, Python memory bounds, attribution identities, pagination, exports, and every Streamlit page. `pip check` found no broken requirements; Python compilation passed. A fresh headless local Streamlit server returned HTTP 200 with health `ok` and was then stopped. The regular launcher remains `start.bat`.

## Synthetic benchmark measurements

All successful rows include reconciliation and reporting. Limits below are DuckDB budgets, not whole-process memory limits. Earlier small runs use intermediate Phase 2 implementations; timings are measurements, not a controlled speedup study.

| Format | Rows per source | Total seconds | Processing seconds | Reporting seconds | Peak process MiB | DuckDB limit |
|---|---:|---:|---:|---:|---:|---|
| CSV | 1,000 | 1.361 | 1.102 | 0.105 | 145.6 | 256MB |
| CSV | 10,000 | 2.250 | 1.920 | 0.100 | 167.2 | 256MB |
| CSV | 100,000 | 12.732 | 12.300 | 0.264 | 288.5 | 256MB |
| CSV | 1,000,000 | 76.392 | 71.177 | 4.909 | 703.1 | 512MB |
| XLSX | 1,000 | 1.985 | 1.727 | 0.109 | 143.1 | 256MB |
| XLSX | 10,000 | 9.106 | 8.752 | 0.110 | 169.0 | 256MB |

The final million-row/source CSV run completed with MATCH in 76.392 seconds and a 512 MB DuckDB budget. The generated joined-detail Parquet file was independently checked to contain exactly 1,000,000 rows. Detailed stage durations and software versions are in `data/phase2_benchmark/summary.json` and the individual run manifests. This covers synthetic sessions with equal fields; it does not establish worst-case performance for millions of field mismatches.

A final background-worker error regression was added after the 75-test full-suite run. All four Streamlit tests then passed, including that new case; there are now 76 distinct verified tests. Early worker input errors remain visible after the completion rerun.
