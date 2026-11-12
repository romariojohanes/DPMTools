# DPM+ Roaming Reconciliation Tool v1.0

Local Windows application for the IOH parallel run between CSG Roaming Legacy (On-Premises) and CSG Roaming SaaS. It reconciles exact `RATED_AMOUNT_TAP` aggregates, matches individual CDRs by configurable service-specific keys, and traces differences to original files and rows. Processing stays local; no CDRs are transmitted to external services.

## Start on this workstation

Double-click **start.bat**, or run it from the project directory. The launcher verifies Python and the locked direct dependency versions before opening **http://localhost:8501**. Keep the console open while working; Ctrl+C stops the server. It binds only to localhost and disables Streamlit usage telemetry. A failed preflight or server start returns a nonzero exit code and leaves an actionable message in the console.

Python 3.12.10 is installed in `.runtime/Python312` on this workstation, and dependencies are installed in `.venv`. These directories are local installation artifacts, excluded from source control. This is a managed-folder release, not a portable EXE; recreate the virtual environment after moving the project.

## Fresh Windows 11 installation

Install 64-bit Python 3.12, then run these commands in PowerShell from this directory:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-lock.txt
.\.venv\Scripts\python.exe -m tools.generate_samples
.\start.bat
```

`requirements.txt` pins the four direct dependencies; `requirements-lock.txt` captures the complete tested dependency environment. Pandas is a Streamlit dependency but application ingestion and reconciliation do not use Pandas. DuckDB reads/writes Apache Parquet directly; PyArrow is installed through Streamlit.

If using the project-local interpreter installed here, replace `py -3.12` with `.\.runtime\Python312\python.exe`. A dependency installation needs internet access; normal reconciliation uses local files and requires no cloud resources.

## Using the application

1. On **New Reconciliation**, select one or more CSV/XLSX files or folders for each source. Native dialogs run on the local Windows desktop. Paths can also be pasted, one per line, including paths with spaces. Folder selection includes immediate CSV/XLSX files, excludes Excel lock files, and does not recurse. Duplicate inputs and reuse of the same path across sources are rejected.
2. Set each source's delimiter, encoding, decimal separator, explicit date format, and optional worksheet. Settings apply to all files within that source for the run; split runs when source file formats differ. Multi-sheet XLSX workbooks require an explicit worksheet selection.
3. In **Column mapping**, click **Inspect source columns** when a source uses nonstandard headers. Assign the normalized source header to each required canonical field. Legacy and SaaS mappings are independent. Recognized canonical, case/separator, and underscore-less aliases can remain automatic. One mapping applies to every file in that source; split runs when files require different mappings. Missing or duplicate assignments are rejected before processing, and the effective mapping is retained in run evidence.
4. Optionally require a single traffic date. Every row outside that date blocks reconciliation. Without a constraint, actual min/max dates and distinct date counts are reported for both sources; review the periods before interpreting results.
5. Start reconciliation. A local background worker reports measured stages, elapsed time, file row counts and throughput while the UI remains usable, and can be cancelled safely. All input issues must be corrected before aggregate results can be published. Failed validation produces an ingestion validation report with original values and provenance. No subset of valid rows is presented as a complete reconciliation.
6. Review **Executive Summary**, **HPMN Comparison**, **VPMN Comparison**, and **HPMN/VPMN Drill-down**. Tables use database filters, sorting, and 100-row pages. Amounts are displayed as decimal strings so browser floating-point numbers cannot alter them.
7. Generate reports on **Export Reports**. Excel workbooks and Parquet details are validated before publication. Verified files up to 25 MB have browser download buttons; larger reports remain at the displayed local path to avoid loading them into Streamlit memory.

Run history persists across restarts. Each run has its own directory and DuckDB database. Set the optional `DPM_RUN_ROOT` environment variable to choose a different run storage directory before launching. One local user/process should operate a run at a time. A run interrupted while processing stays marked `INGESTING`; create a new run rather than treating it as complete.

## Reconciliation levels

| Level | Comparison | Detail |
|---|---|---|
| 1 | SUM(RATED_AMOUNT_TAP) | Per currency; `revenue_status` compares amounts independently of counts |
| 2 | Total CDR count | Every input record, including repeated records |
| 3 | Distinct HPMN count and actual set | Equal counts can still have different operator populations |
| 4 | Distinct VPMN count and actual set | Membership and source presence shown for every operator |
| 5 | CDR count and amount by HPMN | Separate row per HPMN and currency |
| 6 | CDR count and amount by VPMN | Separate row per VPMN and currency |
| 7 | CDR count and amount by HPMN + VPMN + CURRENCY | Operator-pair distribution |

Financial amounts are never added across currencies. All grouped comparisons use full outer joins, retaining Legacy-only and SaaS-only groups even when their amount is zero. `amount_variance = SaaS - Legacy`; absolute variances use `abs(SaaS - Legacy)`. Missing-side values display zero together with explicit presence and mismatch status. Match requires exact equality, with no tolerance. The overall result requires all seven aggregate levels and individual normalized comparisons to agree. Duplicate, incomplete, and unsupported keys make the overall result MISMATCH. Matching aggregates alone do not establish individual CDR equality.

## Input schema and normalization

Every file must resolve to all 24 confirmed fields, either automatically or through the source's explicit mapping. Column names are trimmed, converted to uppercase, and separator characters are normalized to underscores before mapping. Duplicate or blank normalized names, missing targets, and duplicate target assignments are rejected. Additional named columns are ignored. The data row must have the same number of columns as its file's header.

```text
EVENT_DATE
EVENT_BEGIN_TIME
EVENT_END_TIME
TYPE
SERVICE
SERVICE_TYPE
IMSI
VPMN
HPMN
VOLUME_INCOMING
VOLUME_OUTGOING
VOLUME_TOTAL
VOLUME_CHARGED_UNITS
DURATION_TOTAL
CURRENCY
RATED_AMOUNT_TAP
TAX_AMOUNT_TAP
FINAL_TAX_AMOUNT
RATE
RATE_UNIT
ROUNDED_UNIT_ID
GPRS_CHARGING_ID
ANUM
BNUM
```

| Fields | Policy |
|---|---|
| EVENT_DATE | DATE, using selected `%Y-%m-%d`, `%Y%m%d`, `%d/%m/%Y`, or `%m/%d/%Y`; exact format round-trip required |
| EVENT_BEGIN_TIME, EVENT_END_TIME | Recognized HHMMSS, native integral times, and H:MM:SS normalize to HH:MM:SS; other strings are retained. No duration derivation |
| IMSI, ANUM, BNUM, GPRS_CHARGING_ID | Strings; text leading zeros retained. Nonnegative integral Excel numeric cells with at most 15 digits are accepted exactly; unsafe numeric cells block validation |
| HPMN, VPMN | Trimmed, uppercase strings; required |
| CURRENCY | Trimmed, uppercase, required three-letter code; syntax check, not an ISO currency registry lookup |
| RATED_AMOUNT_TAP | Required exact DECIMAL(38, scale), default scale 12 |
| VOLUME_INCOMING, VOLUME_OUTGOING, VOLUME_TOTAL, VOLUME_CHARGED_UNITS, DURATION_TOTAL, TAX_AMOUNT_TAP, FINAL_TAX_AMOUNT, RATE | Nullable exact DECIMAL(38, scale); invalid non-null values block the run |
| Remaining fields | Trimmed nullable strings |

Blank/whitespace values and case-insensitive `NULL`, `NONE`, `N/A`, `NA`, and `\N` normalize to null. EVENT_DATE, CURRENCY, HPMN, VPMN, and RATED_AMOUNT_TAP cannot be null. A zero financial amount is valid. Negative amounts are retained.

Scale can be set from 0 to 30 before ingestion. With scale 12, DECIMAL(38,12) allows up to 26 integer digits. More fractional digits are rejected, including extra trailing zeros. Increase scale explicitly if needed, noting that this reduces available integer digits. Scientific notation is expanded by exact string arithmetic. NaN/Infinity, grouping separators, and ambiguous numeric formats are rejected. Arithmetic and aggregate overflow fail the run rather than silently rounding. Dot and comma decimal modes are explicit; in comma mode, `12,34` is accepted and `12.34` is rejected. A comma-delimited CSV containing comma-decimal values must quote those fields; semicolon CSV is usually easier.

XLSX formulas are rejected, including formulas outside the financial field. Dates stored as Excel dates are accepted; non-midnight date/time cells in EVENT_DATE are rejected. Native Excel numeric financial cells are converted from their stored decimal representation without additional floating-point arithmetic; small native numbers are expanded from exponent notation and do not depend on the selected locale separator. Digits already lost to Excel's numeric precision cannot be recovered: supply financial values as text when high precision matters. Numeric identifiers exceeding 15 digits, negative/nonintegral identifiers and unsafe scientific forms are reported as POTENTIALLY_CORRUPTED_IDENTIFIER. Already-lost leading zeros or rounded digits cannot be recovered; verify against original source text when uncertain.

## Provenance and validation

Normalized records include `source_file` (full original path), `source_sheet`, and `source_row`. Source filenames, file sizes, modification timestamps, SHA-256 digests, verification status, and ingestion row counts are recorded in `run.json` and every report's Source Files sheet. Each input is hashed before and after ingestion; a digest change blocks reconciliation.

For CSV, `source_row` is the **physical starting line in the original file**, including the header and blank lines. Quoted multiline fields are supported. A bounded streaming pass creates a disk index of row locations; DuckDB performs the CSV ingestion and data conversion. Completely empty physical lines are not CDRs. Malformed CSV structures reject the entire file with a diagnostic; files are never scanned with `ignore_errors`. Invalid values in well-formed records retain field-level raw values and locations. An unparseable file remains referenced by its full original path and file-level error; no partial record extraction is claimed.

For XLSX, `source_row` is the worksheet row number. Completely empty worksheet rows are not CDRs. A workbook is ingested atomically through read-only openpyxl row iterators and a streaming disk spool scanned by DuckDB. The shared-string dictionary is indexed on disk using Python standard-library SQLite with a 4 MB page cache and a 4,096-entry text cache; it is not loaded wholesale into Python memory. Keep input files stable throughout processing: a size or modification-time change blocks the run. This is a stability check, not a cryptographic content fingerprint.

Original strings are retained in `raw_legacy` and `raw_saas` in DuckDB for parsed files. Original source files are not copied or modified. Native Excel numeric raw values preserve the original XML numeric tokens, before openpyxl float conversion; dates preserve their calendar value. Display formatting is not treated as identifier content. Correct invalid source files and create a new run; there is no row exclusion or validation override workflow.

## Reports

Excel reports include Executive Summary, Operator Comparison, the existing HPMN/VPMN reports, Data Ingestion Validation, CDR Matching Summary, Rating Mismatch, Legacy Only, SaaS Only, Duplicate Ambiguous, Field-Level Differences, and Revenue Variance Attribution.

All reports include run metadata and source filenames. Financial decimals are exported as exact text. Input strings are forced to Excel text to prevent formula interpretation. Write-only Excel output uses bounded cursor batches, limits each table to 50,000 rows, and respects Excel worksheet row limits. Complete detail exports use DuckDB-native streaming CSV or Parquet; large files stay on disk. `export_detail(..., filters={...})` supports parameterized filters; UI pagination and sorting also run in DuckDB. Excel extracts are for review and do not represent complete multi-million-row detail.

## Individual CDR matching (levels 8?10)

Profiles are configured as a SERVICE_TYPE-to-key-fields JSON mapping in New Reconciliation. GPRS starts with IMSI, EVENT_DATE, EVENT_BEGIN_TIME, EVENT_END_TIME, VPMN, HPMN and GPRS_CHARGING_ID. Generic VOICE/SMS profiles include both ANUM and BNUM; direction-specific VOICE-MO/SMS-MO profiles use ANUM and VOICE-MT/SMS-MT profiles use BNUM. These are candidate keys requiring business review for each source; uniqueness is checked independently on both sides. Rating-related fields cannot be keys.

Keys use collision-free JSON serialization of normalized values and service type. Source order is irrelevant. Only keys with exactly one record on each side and every key field populated are paired. Duplicate groups, missing key values, and unsupported service types remain DUPLICATE_OR_AMBIGUOUS. Duplicate members are retained individually, never cross-joined or arbitrarily paired. `duplicate_keys` reports per-side uniqueness counts, while `ambiguous_records` contains every original member and its provenance.

The other classifications are MATCHED_SAME_RATING, MATCHED_DIFFERENT_RATING, LEGACY_ONLY and SAAS_ONLY. Same rating requires both RATED_AMOUNT_TAP and CURRENCY equality. Primary and secondary fields are compared after unique matching; optional full audit covers all 24 fields. Field differences distinguish RAW_FORMAT_ONLY from NORMALIZED_VALUE, and show original/normalized values, source references and exact numeric differences. Monetary differences across different currencies are left null.

Revenue attribution is independent per currency: SaaS minus Legacy equals matched rating differences + SaaS-only amounts - Legacy-only amounts + unresolved group differences. A nonzero residual fails reconciliation. An attribution status MATCH certifies this identity; it does not resolve duplicate CDRs. The aggregate result is also saved separately as `aggregate_result`.

All existing pages remain. Additional pages expose matching, missing records, rating and field differences, duplicates and attribution. Select an operator comparison row and use **Investigate selected CDRs** to open matching details with HPMN/VPMN filters. Historical Phase 1 runs remain readable and are explicitly identified as lacking CDR matching.

## Storage, memory, and performance limits

The target is up to 17 million CDRs per source per day on Windows 11, Intel Core i5, 32 GB RAM. **This capacity has not been validated.** The engine provides disk-backed processing and repeatable benchmarks, not a production throughput guarantee.

DuckDB starts with a 4 GB memory limit and four threads, configurable per run. Normalization uses deterministic 250,000-row chunks. Saved-run investigation, wide comparison, attribution and export queries use one worker to bound join buffers. The DuckDB limit does not cover every allocation in DuckDB, Python, openpyxl, or Streamlit. The application also enforces process-memory, available-memory, temporary-storage and free-disk guards; breached runs are interrupted without publishing a partial result. XLSX throughput is lower than CSV and has not been established at the target scale. Prefer CSV for large extracts.

Successful runs store normalized datasets as ZSTD-compressed Parquet and aggregate tables/raw values in DuckDB. During processing, both normalized DuckDB tables and Parquet may temporarily coexist. Existing runs reference absolute Parquet paths; keep a run in its original location, or re-ingest after moving it. Retention and cleanup are manual; stop the app before deleting a run directory. Local outputs can contain input identifiers; store them in an appropriately controlled local workspace.

The interface fetches at most 100 rows per page, and operator filters run in SQL. Only bounded result pages reach Streamlit. Excel exports stream through 2,000-row query batches, while complete detail exports run in a background worker. This does not include database locking for multiple users, authenticated hosting, resume/recovery, or production retention automation.

Run a repeatable synthetic benchmark:

```powershell
.\.venv\Scripts\python.exe -m tools.benchmark --rows 100000
```

Results are written to `data/benchmark/benchmark_result.json`. The elapsed time includes ingestion, normalization, aggregates, matching, field comparison, attribution, Parquet detail and bounded Excel summary export. Stage timings and peak native process memory are recorded where measurable. Generation and UI rendering are excluded from elapsed time; process peak memory includes generation. Use --format xlsx for workbook measurements. Results from this small dataset must not be extrapolated to 17 million rows without representative scale and resource measurements. Use a fresh benchmark directory for separate runs, and review disk availability before substantially increasing row counts.

The initial Windows smoke benchmark processed 100,000 rows per source (200,000 total) in **11.163 seconds**, with a MATCH result, 16 GB DuckDB memory limit, and four threads. It did not measure peak RAM or disk usage. That historical Phase 1 run excluded CDR matching and reporting and is not a directly comparable full Phase 2 baseline. Current verification and benchmark results are documented in PHASE2_VALIDATION.md.

## Synthetic samples and testing

All sample identities/operators are fabricated (`TEST_*`, `TST*`); no actual subscriber data is used.

| Directory | Expected result |
|---|---|
| samples/matching | MATCH; Legacy CSV and SaaS XLSX, USD and IDR |
| samples/variance | MISMATCH; revenue and operator differences |
| samples/invalid | VALIDATION_FAILED; invalid RATED_AMOUNT_TAP |
| samples/comma_decimal | MATCH with delimiter `;` and decimal separator `,` for both sources |
| samples/multiple_files | MATCH; select the legacy and saas directories, each containing CSV and XLSX |

Generate samples and run the automated checks:

```powershell
.\.venv\Scripts\python.exe -m tools.generate_samples
.\.venv\Scripts\python.exe -m pytest -q --basetemp .pytest_tmp
.\.venv\Scripts\python.exe -m tools.release_check --preflight
.\.venv\Scripts\python.exe -m pip check
```

To create a synthetic matching run and all report files for manual review:

```powershell
.\.venv\Scripts\python.exe -m tools.verify_samples
```

The generated run appears in the sidebar history. The script records its output paths in `data/sample_verification.json`.

Before publishing a managed-folder release, complete every item in `RELEASE_CHECKLIST.md`. A release check never installs or upgrades packages and never modifies historical benchmark evidence.

Tests cover all seven levels, exact decimal precision, invalid financial values, mismatched counts, same-count/different operator sets, missing/additional operators, currency isolation, multiple files, CSV/XLSX, encoding and decimal formats, dates, raw provenance, malformed files, unsafe Excel cells, all reports, formula-safe exports, database filtering, and Streamlit navigation.

## Architecture

```text
app.py                      Streamlit pages, bounded SQL pagination, native picker entry points
core/schema.py              Confirmed fields, explicit settings, path/schema validation
core/ingestion.py           CSV/XLSX parsing, provenance, normalization and issue capture
core/xlsx_stream.py         Bounded openpyxl shared-string dictionary and workbook lifecycle
core/matching.py            Composite keys, duplicate handling, field comparison and revenue attribution
core/engine.py              Isolated run database, settings and bounded result access
core/reconciliation.py      Seven aggregate levels; full outer joins and set comparisons
core/workflow.py            Run lifecycle, validation gate and Parquet publication
core/reporting.py           Bounded exact-text Excel and native CSV/Parquet exports
tools/generate_samples.py   Reproducible fabricated sample files
tools/benchmark.py          Opt-in synthetic CSV/XLSX benchmark and memory measurement
tools/phase3a_benchmark.py  Staged scenarios, resource gates and complete output verification
tests/                      Automated engine, reporting and Streamlit tests
data/runs/<run_id>/          Local run metadata, DuckDB, Parquet, reports and temporary files
```

The normalized 24-field schema, raw values, and retained provenance are preserved. No new third-party dependencies are required. See [PHASE2_VALIDATION.md](PHASE2_VALIDATION.md) for prior evidence and [PHASE3A_READINESS.md](PHASE3A_READINESS.md) for staged benchmark gates. No 17-million-record capability is claimed until Stage 4 passes twice.
