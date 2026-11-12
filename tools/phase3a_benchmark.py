"""Deterministic, safety-gated benchmark runner.

Generation, processing, verification, and exports are explicit operations. The
runner refuses to overwrite evidence and never treats a partial run as passed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import time
from uuid import uuid4
from decimal import Decimal
from pathlib import Path
from threading import Event
from time import monotonic, perf_counter, process_time

import duckdb
from openpyxl import load_workbook

from core.engine import Run
from core.monitoring import GIB, ResourceMonitor, _directory_bytes, _windows_memory_io
from core.reporting import DETAIL_TABLES, REPORTS, export_detail, export_report
from core.schema import MATCH_PROFILES, Settings
from core.workflow import execute
from tools.generate_samples import cdr, write_csv, write_xlsx

STAGES = {1: 2_000_000, 2: 5_000_000, 3: 10_000_000, 4: 17_000_000}
TEMP_CAPS = {1: '20GB', 2: '35GB', 3: '55GB', 4: '75GB'}
TIME_CAPS = {1: 30 * 60, 2: 60 * 60, 3: 120 * 60, 4: 240 * 60}
REQUIRED_FREE_GB = {1: 75, 2: 95, 3: 125, 4: 155}
SCENARIOS = (
    'identical', 'reordered', 'rating_difference', 'missing_additional',
    'duplicate_ambiguous', 'mixed', 'high_cardinality_mismatch',
)
SCENARIO_ALIASES = {'high_mismatch': 'high_cardinality_mismatch'}
DECIMAL_QUANTUM = Decimal('0.000000000001')
BENCHMARK_PROFILES = tuple(MATCH_PROFILES)


def residue_count(rows, modulus, residue):
    return 0 if rows <= residue else (rows - 1 - residue) // modulus + 1


def _scenario(value):
    return SCENARIO_ALIASES.get(value, value)


def _flags(scenario):
    scenario = _scenario(scenario)
    return {
        'missing': scenario in ('missing_additional', 'mixed'),
        'duplicates': scenario in ('duplicate_ambiguous', 'mixed'),
        'rating': scenario in ('rating_difference', 'mixed'),
        'rate': scenario == 'mixed',
        'high_cardinality': scenario == 'high_cardinality_mismatch',
    }


def _decimal_text(value):
    return format(Decimal(value).quantize(DECIMAL_QUANTUM), 'f')


def expected(rows, scenario):
    """Return the complete deterministic oracle for a synthetic fixture."""
    scenario = _scenario(scenario)
    flags = _flags(scenario)
    missing_legacy = residue_count(rows, 1000, 1) if flags['missing'] else 0
    missing_saas = residue_count(rows, 1000, 2) if flags['missing'] else 0
    duplicates = (residue_count(rows, 1000, 3) + residue_count(rows, 1000, 4)) if flags['duplicates'] else 0
    rating = residue_count(rows, 1000, 5) if flags['rating'] else 0
    rate = residue_count(rows, 1000, 6) if flags['rate'] else 0
    high = rows if flags['high_cardinality'] else 0
    same = rows - missing_legacy - missing_saas - duplicates - rating
    legacy_rows = rows - missing_legacy + duplicates
    saas_rows = rows - missing_saas + duplicates
    legacy_total = Decimal(legacy_rows)
    saas_total = Decimal(saas_rows) + Decimal(rating) / 2
    variance = saas_total - legacy_total
    field_counts = {}
    if rating:
        field_counts['RATED_AMOUNT_TAP:NORMALIZED_VALUE'] = rating
    if rate or high:
        field_counts['RATE:NORMALIZED_VALUE'] = rate + high
    statuses = {
        'MATCHED_SAME_RATING': same,
        'MATCHED_DIFFERENT_RATING': rating,
        'LEGACY_ONLY': missing_saas,
        'SAAS_ONLY': missing_legacy,
        'DUPLICATE_OR_AMBIGUOUS': duplicates,
    }
    return {
        **statuses,
        'scenario': scenario,
        'source_rows': {'legacy': legacy_rows, 'saas': saas_rows},
        'classification_records': {
            'legacy': rows - missing_legacy + duplicates,
            'saas': rows - missing_saas + duplicates,
        },
        'classification_record_counts': {
            'MATCHED_SAME_RATING': {'legacy': same, 'saas': same},
            'MATCHED_DIFFERENT_RATING': {'legacy': rating, 'saas': rating},
            'LEGACY_ONLY': {'legacy': missing_saas, 'saas': 0},
            'SAAS_ONLY': {'legacy': 0, 'saas': missing_legacy},
            'DUPLICATE_OR_AMBIGUOUS': {'legacy': duplicates * 2, 'saas': duplicates * 2},
        },
        'profiles': list(BENCHMARK_PROFILES[:min(rows, len(BENCHMARK_PROFILES))]),
        'ambiguous_legacy_records': duplicates * 2,
        'ambiguous_saas_records': duplicates * 2,
        'field_differences': sum(field_counts.values()),
        'field_difference_counts': field_counts,
        'financial': {
            'currency': 'USD',
            'legacy_total': _decimal_text(legacy_total),
            'saas_total': _decimal_text(saas_total),
            'variance': _decimal_text(variance),
            'matched_rating_differences': _decimal_text(Decimal(rating) / 2),
            'saas_only_amount': _decimal_text(Decimal(missing_legacy)),
            'negative_legacy_only_amount': _decimal_text(-Decimal(missing_saas)),
            'unresolved_ambiguous_difference': _decimal_text(0),
            'residual': _decimal_text(0),
        },
        'result': 'MATCH' if scenario in ('identical', 'reordered') else 'MISMATCH',
    }


def _values(index, source, scenario):
    flags = _flags(scenario)
    values = dict(
        IMSI=f'TEST_IMSI_{index:017d}', GPRS_CHARGING_ID=f'{index:020d}',
        HPMN=f'TST{index % 50:02d}', VPMN=f'TST{(index * 7) % 70:02d}',
        SERVICE_TYPE=BENCHMARK_PROFILES[index % len(BENCHMARK_PROFILES)],
        RATED_AMOUNT_TAP='1.000000000000',
    )
    residue = index % 1000
    if source == 'saas' and flags['rating'] and residue == 5:
        values['RATED_AMOUNT_TAP'] = '1.500000000000'
    if source == 'saas' and flags['rate'] and residue == 6:
        values['RATE'] = '0.000200000000'
    if source == 'saas' and flags['high_cardinality']:
        values['RATE'] = f'0.{200_000_000 + index:012d}'
    return cdr(**values)


def benchmark_rows(rows, source, scenario, reverse=False):
    flags = _flags(scenario)
    sequence = range(rows - 1, -1, -1) if reverse else range(rows)
    for index in sequence:
        residue = index % 1000
        if flags['missing'] and ((source == 'legacy' and residue == 1) or (source == 'saas' and residue == 2)):
            continue
        row = _values(index, source, scenario)
        yield row
        if flags['duplicates'] and residue in (3, 4):
            yield dict(row)


def _sha256(path, checkpoint=None):
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        blocks = 0
        while block := handle.read(4 * 1024 * 1024):
            digest.update(block)
            blocks += 1
            if checkpoint is not None and blocks % 64 == 0:
                checkpoint()
    return digest.hexdigest()


def _atomic_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, indent=2, default=str), encoding='utf-8')
    temporary.replace(path)


class GenerationGuard:
    """Low-overhead safety checks while synthetic files are being written."""

    def __init__(self, root, settings, cancel_event=None, required_free_gb=None):
        self.root = Path(root)
        self.settings = settings
        self.cancel_event = cancel_event
        self.required_free_gb = required_free_gb or settings.free_space_reserve_gb
        self.started = monotonic()
        self.previous_wall = self.started
        self.previous_cpu = process_time()
        self.last_sample = 0.0
        self.samples = []

    def check(self, stage='generation', force=False):
        now = monotonic()
        if not force and now - self.last_sample < self.settings.monitor_interval_seconds:
            return
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise RuntimeError('Benchmark generation cancelled by user.')
        memory = _windows_memory_io() or {}
        disk = shutil.disk_usage(self.root if self.root.exists() else self.root.parent)
        cpu = process_time()
        interval = now - self.previous_wall
        cpu_percent = None if interval < 0.01 else max(
            0.0, (cpu - self.previous_cpu) / interval * 100 / max(os.cpu_count() or 1, 1))
        sample = {
            **memory, 'elapsed_seconds': round(now - self.started, 3), 'stage': stage,
            'generated_bytes': _directory_bytes(self.root), 'disk_free_bytes': disk.free,
            'cpu_percent': round(cpu_percent, 2) if cpu_percent is not None else None,
        }
        previous = self.samples[-1] if self.samples else None
        if previous and interval >= 0.01:
            for key in ('read_bytes', 'write_bytes'):
                if memory.get(key) is not None and previous.get(key) is not None:
                    sample[key + '_per_second'] = max(0, round((memory[key] - previous[key]) / interval, 2))
        self.samples.append(sample)
        self.last_sample = now
        self.previous_wall, self.previous_cpu = now, cpu
        if memory.get('working_set_bytes', 0) > self.settings.process_memory_limit_gb * GIB:
            raise RuntimeError('Generation resource limit reached: PROCESS_MEMORY_LIMIT.')
        if memory.get('available_memory_bytes', GIB) < self.settings.available_memory_floor_gb * GIB:
            raise RuntimeError('Generation resource limit reached: AVAILABLE_MEMORY_FLOOR.')
        if disk.free < self.required_free_gb * GIB:
            raise RuntimeError('Generation resource limit reached: PROJECTED_FREE_SPACE_REQUIREMENT.')
        if self.settings.max_elapsed_seconds is not None and sample['elapsed_seconds'] > self.settings.max_elapsed_seconds:
            raise RuntimeError('Generation resource limit reached: ELAPSED_TIME_LIMIT.')

    def finish(self, filename='generation_resource_samples.json'):
        self.check(force=True)
        _atomic_json(self.root / filename, self.samples)


def _write_partitioned(folder, source, fmt, rows, scenario, rows_per_file, guard=None):
    folder.mkdir(parents=True, exist_ok=True)
    paths = []
    reverse = _scenario(scenario) == 'reordered' and source == 'saas'
    total_files = (rows + rows_per_file - 1) // rows_per_file
    for part in range(total_files):
        count = min(rows_per_file, rows - part * rows_per_file)
        if reverse:
            start = rows - part * rows_per_file - 1
            indices = range(start, start - count, -1)
        else:
            start = part * rows_per_file
            indices = range(start, start + count)
        actual = 0

        def selected():
            nonlocal actual
            flags = _flags(scenario)
            for index in indices:
                residue = index % 1000
                if flags['missing'] and ((source == 'legacy' and residue == 1) or (source == 'saas' and residue == 2)):
                    continue
                row = _values(index, source, scenario)
                actual += 1
                if guard and actual % 10_000 == 0:
                    guard.check()
                yield row
                if flags['duplicates'] and residue in (3, 4):
                    actual += 1
                    yield dict(row)

        suffix = 'xlsx' if fmt == 'xlsx' else 'csv'
        path = folder / f'{source}_{part + 1:03d}.{suffix}'
        (write_xlsx if fmt == 'xlsx' else write_csv)(path, selected())
        paths.append({'path': str(path.resolve()), 'bytes': path.stat().st_size, 'rows': actual,
                      'sha256': _sha256(path, guard.check if guard else None)})
        if guard:
            guard.check(force=True)
    return paths


def generate(root, rows, scenario, fmt, rows_per_file, guard=None):
    scenario = _scenario(scenario)
    inputs = Path(root) / 'inputs'
    records = {}
    for source in ('legacy', 'saas'):
        source_format = 'csv' if fmt == 'cross' and source == 'legacy' else ('xlsx' if fmt == 'cross' else fmt)
        records[source] = _write_partitioned(inputs / source, source, source_format, rows, scenario, rows_per_file, guard)
    manifest = {
        'schema_version': 2, 'rows_per_source_basis': rows, 'scenario': scenario, 'format': fmt,
        'files': {source: [item['path'] for item in values] for source, values in records.items()},
        'file_integrity': records, 'expected': expected(rows, scenario),
    }
    _atomic_json(Path(root) / 'fixture_manifest.json', manifest)
    return manifest


def validate_manifest(manifest, *, stage=None, root=None, checkpoint=None):
    errors = []
    if manifest.get('schema_version') != 2:
        errors.append('fixture manifest schema_version must be 2')
    rows = manifest.get('rows_per_source_basis')
    scenario = _scenario(manifest.get('scenario'))
    if not isinstance(rows, int) or rows < 1:
        errors.append('rows_per_source_basis must be positive')
    if scenario not in SCENARIOS:
        errors.append(f'unsupported scenario: {scenario}')
    if stage is not None and rows != STAGES[stage]:
        errors.append(f'stage {stage} requires {STAGES[stage]} rows per source, manifest has {rows}')
    if manifest.get('expected') != (expected(rows, scenario) if isinstance(rows, int) and rows > 0 and scenario in SCENARIOS else None):
        errors.append('deterministic expected oracle does not match manifest parameters')
    for source in ('legacy', 'saas'):
        paths = manifest.get('files', {}).get(source, [])
        records = manifest.get('file_integrity', {}).get(source, [])
        if len(paths) != len(records) or not paths:
            errors.append(f'{source} file and integrity entries disagree or are empty')
            continue
        for path_text, record in zip(paths, records):
            path = Path(path_text).resolve()
            if str(path) != record.get('path'):
                errors.append(f'{source} integrity path mismatch: {path}')
                continue
            if root is not None and Path(root).resolve() not in path.parents:
                errors.append(f'{source} input is outside benchmark directory: {path}')
            if not path.is_file():
                errors.append(f'{source} input missing: {path}')
                continue
            if path.stat().st_size != record.get('bytes'):
                errors.append(f'{source} input byte size changed: {path}')
            if _sha256(path, checkpoint) != record.get('sha256'):
                errors.append(f'{source} input SHA-256 changed: {path}')
        if sum(item.get('rows', -1) for item in records) != manifest.get('expected', {}).get('source_rows', {}).get(source):
            errors.append(f'{source} manifest row total does not match oracle')
    if errors:
        raise ValueError('Invalid benchmark fixture manifest: ' + '; '.join(errors))
    return manifest


def _actual_decimal(value):
    return _decimal_text(Decimal(value or 0))


def verify_run(run, expected_values, manifest=None):
    actual = {row['status']: row['groups'] for row in run.rows(
        'SELECT status,count(*) AS groups FROM cdr_matches GROUP BY status')}
    errors = []
    statuses = ('MATCHED_SAME_RATING', 'MATCHED_DIFFERENT_RATING', 'LEGACY_ONLY', 'SAAS_ONLY', 'DUPLICATE_OR_AMBIGUOUS')
    for status in statuses:
        wanted = expected_values.get(status, 0)
        if actual.get(status, 0) != wanted:
            errors.append(f'{status}: expected {wanted}, found {actual.get(status, 0)}')
    record_counts = {
        row['status']: {'legacy': row['legacy_records'], 'saas': row['saas_records']}
        for row in run.rows('''SELECT status,sum(legacy_count) AS legacy_records,
                                      sum(saas_count) AS saas_records
                               FROM cdr_matches GROUP BY status''')
    }
    for status in statuses:
        found = record_counts.get(status, {'legacy': 0, 'saas': 0})
        wanted = expected_values['classification_record_counts'][status]
        if found != wanted:
            errors.append(f'{status} source records: expected {wanted}, found {found}')
    profiles = sorted(row['profile'] for row in run.rows('SELECT DISTINCT profile FROM cdr_matches'))
    if profiles != sorted(expected_values['profiles']):
        errors.append(f'matching profiles: expected {expected_values["profiles"]}, found {profiles}')
    source_rows = {}
    for source in ('legacy', 'saas'):
        source_rows[source] = run.con.execute(f'SELECT count(*) FROM raw_{source}').fetchone()[0]
        if source_rows[source] != expected_values['source_rows'][source]:
            errors.append(f'{source} rows: expected {expected_values["source_rows"][source]}, found {source_rows[source]}')
    ambiguous = {row['source']: row['records'] for row in run.rows(
        'SELECT source,count(*) AS records FROM ambiguous_records GROUP BY source')}
    for source in ('legacy', 'saas'):
        wanted = expected_values[f'ambiguous_{source}_records']
        if ambiguous.get(source, 0) != wanted:
            errors.append(f'ambiguous {source} records: expected {wanted}, found {ambiguous.get(source, 0)}')
    differences = {f'{row["field"]}:{row["difference_type"]}': row['records'] for row in run.rows(
        'SELECT field,difference_type,count(*) AS records FROM field_differences GROUP BY field,difference_type')}
    if differences != expected_values['field_difference_counts']:
        errors.append(f'field differences: expected {expected_values["field_difference_counts"]}, found {differences}')
    financial = expected_values['financial']
    revenue = run.rows('SELECT * FROM revenue WHERE CURRENCY=?', [financial['currency']])
    if len(revenue) != 1:
        errors.append(f'expected one {financial["currency"]} revenue row, found {len(revenue)}')
    else:
        for actual_name, expected_name in (('legacy_amount', 'legacy_total'), ('saas_amount', 'saas_total'), ('amount_variance', 'variance')):
            if _actual_decimal(revenue[0][actual_name]) != financial[expected_name]:
                errors.append(f'{actual_name}: expected {financial[expected_name]}, found {_actual_decimal(revenue[0][actual_name])}')
    attribution = run.rows('SELECT * FROM revenue_attribution WHERE CURRENCY=?', [financial['currency']])
    if len(attribution) != 1:
        errors.append(f'expected one {financial["currency"]} attribution row, found {len(attribution)}')
    else:
        for name in ('matched_rating_differences', 'saas_only_amount', 'negative_legacy_only_amount',
                     'unresolved_ambiguous_difference', 'residual'):
            if _actual_decimal(attribution[0][name]) != financial[name]:
                errors.append(f'{name}: expected {financial[name]}, found {_actual_decimal(attribution[0][name])}')
        if attribution[0]['status'] != 'MATCH':
            errors.append('revenue attribution status is not MATCH')
    if run.meta.get('result') != expected_values['result']:
        errors.append(f'result: expected {expected_values["result"]}, found {run.meta.get("result")}')
    if manifest is not None:
        approved = {item['path']: item for values in manifest['file_integrity'].values() for item in values}
        for record in run.meta.get('files', []):
            fixture = approved.get(record['path'])
            if not fixture or not record.get('sha256_verified') or record.get('sha256_before') != fixture['sha256']:
                errors.append(f'input integrity failed for {record.get("path")}')
    return {
        'passed': not errors, 'errors': errors, 'matching_groups': actual,
        'source_rows': source_rows, 'ambiguous_records': ambiguous,
        'field_difference_counts': differences, 'financial': financial,
    }


def _verify_xlsx(path, run, report):
    workbook = load_workbook(path, read_only=True, data_only=False)
    try:
        if 'Run Metadata' not in workbook.sheetnames or 'Source Files' not in workbook.sheetnames:
            raise ValueError(f'Excel report lacks required evidence sheets: {path}')
        for row in workbook['Source Files'].iter_rows(min_row=2, values_only=True):
            if row and row[0] is not None:
                break
        else:
            raise ValueError(f'Excel report has no source evidence rows: {path}')
        if 'Extract Coverage' not in workbook.sheetnames:
            raise ValueError(f'Excel report lacks extract coverage: {path}')
        rows = workbook['Extract Coverage'].iter_rows(values_only=True)
        headers = next(rows)
        coverage = {record[0]: dict(zip(headers, record)) for record in rows if record and record[0] is not None}
        for table in REPORTS[report]:
            total = run.con.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
            found = coverage.get(table)
            if (found is None or found['available_rows'] != total
                    or found['exported_rows'] != min(total, 50_000)
                    or bool(found['truncated']) != (total > 50_000)):
                raise ValueError(f'Excel extract coverage mismatch for {table}: {path}')
    finally:
        workbook.close()


def _write_evidence_manifest(root, result):
    paths = [Path(root) / 'fixture_manifest.json', Path(root) / 'generation_resource_samples.json',
             Path(root) / 'manifest_validation_resource_samples.json',
             Path(root) / 'benchmark_result.json']
    if result.get('run_directory'):
        paths[:0] = [Path(result['run_directory']) / 'run.json',
                     Path(result['run_directory']) / 'resource_samples.jsonl']
    evidence = {'schema_version': 1, 'files': []}
    for path in paths:
        if path.is_file():
            evidence['files'].append({'path': str(path.resolve()), 'bytes': path.stat().st_size, 'sha256': _sha256(path)})
    # Complete outputs were already hashed twice under the active export monitor.
    evidence['files'].extend({key: item[key] for key in ('path', 'bytes', 'sha256')}
                             for item in result.get('exports', []))
    _atomic_json(Path(root) / 'evidence_manifest.json', evidence)


def _publish_verified_exports(run_directory, staging, worker_result):
    """Promote verified artifacts, publishing the completion manifest last."""
    staging, reports = Path(staging), Path(run_directory) / 'reports'
    reports.mkdir(parents=True, exist_ok=True)
    published = []

    def progress(event, **details):
        record = {'event': event, **details}
        with (staging / 'export_progress.jsonl').open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(record, separators=(',', ':')) + '\n')
            handle.flush()
            os.fsync(handle.fileno())
        _atomic_json(staging / 'export_progress.json', record)

    progress('publication_start', artifacts=len(worker_result['exports']))
    for item in worker_result['exports']:
        source = staging / item['filename']
        if (not source.is_file() or source.stat().st_size != item['bytes']
                or _sha256(source) != item['sha256']):
            raise ValueError(f'Staged export integrity failed: {source}')
        target = reports / source.name
        source.replace(target)
        published.append({**item, 'path': str(target.resolve())})
        progress('artifact_published', filename=target.name, bytes=item['bytes'], sha256=item['sha256'])
    manifest = {'schema_version': 1, 'status': 'COMPLETED', 'exports': published}
    _atomic_json(reports / 'completed_export_manifest.json', manifest)
    progress('publication_complete', manifest=str((reports / 'completed_export_manifest.json').resolve()))
    return published


def _run_export_worker(root, run_directory, deadline, cancel_event=None, *, poll_seconds=0.2,
                       termination_grace_seconds=5.0, worker_command=None):
    """Run all exports out of process and enforce an absolute parent deadline."""
    staging = Path(run_directory) / 'export_staging' / uuid4().hex
    cancel_file = staging.with_suffix('.cancel')
    command = worker_command or [
        sys.executable, '-m', 'tools.export_worker', '--run-directory', str(run_directory),
        '--staging', str(staging), '--cancel-file', str(cancel_file)]
    process = subprocess.Popen(command)
    reason = None
    while process.poll() is None:
        if cancel_event is not None and cancel_event.is_set():
            reason = 'USER_CANCELLED'
            break
        if monotonic() >= deadline:
            reason = 'ELAPSED_TIME_LIMIT'
            break
        time.sleep(min(poll_seconds, max(0.01, deadline - monotonic())))
    if reason is not None and process.poll() is None:
        cancel_file.parent.mkdir(parents=True, exist_ok=True)
        cancel_file.touch(exist_ok=True)
        grace_end = monotonic() + termination_grace_seconds
        while process.poll() is None and monotonic() < grace_end:
            time.sleep(min(poll_seconds, max(0.01, grace_end - monotonic())))
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=termination_grace_seconds)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=termination_grace_seconds)
    return_code = process.wait()
    if reason is not None or return_code != 0:
        staging.mkdir(parents=True, exist_ok=True)
        failure = {'status': 'INCOMPLETE', 'reason': reason or 'WORKER_FAILED',
                   'return_code': return_code, 'staging': str(staging)}
        _atomic_json(staging / 'incomplete_manifest.json', failure)
        raise RuntimeError(f'Complete export worker failed: {failure["reason"]}.')
    result_path = staging / 'worker_result.json'
    if not result_path.is_file():
        raise RuntimeError('Complete export worker exited without a verified result.')
    result = json.loads(result_path.read_text(encoding='utf-8'))
    if result.get('status') != 'VERIFIED':
        raise RuntimeError('Complete export worker did not verify its artifacts.')
    return _publish_verified_exports(run_directory, staging, result)


def run_benchmark(root, manifest, stage, memory, threads, complete_exports, *, temp_directory=None,
                  monitor_interval=2.0, cancel_event=None, max_elapsed_seconds=None,
                  free_space_reserve_gb=50, process_memory_limit_gb=14, available_memory_floor_gb=4):
    started = perf_counter()
    settings = Settings(
        memory_limit=memory, threads=threads, max_temp_directory_size=TEMP_CAPS[stage],
        temp_directory=temp_directory, monitor_interval_seconds=monitor_interval,
        max_elapsed_seconds=max_elapsed_seconds or TIME_CAPS[stage], free_space_reserve_gb=free_space_reserve_gb,
        process_memory_limit_gb=process_memory_limit_gb, available_memory_floor_gb=available_memory_floor_gb,
    )
    certification_stage = stage if manifest['rows_per_source_basis'] == STAGES.get(stage) else None
    manifest_guard = GenerationGuard(
        root, settings, cancel_event,
        REQUIRED_FREE_GB[stage] if certification_stage is not None else free_space_reserve_gb,
    )
    try:
        manifest_guard.check(stage='manifest_validation', force=True)
        validate_manifest(manifest, stage=certification_stage, root=root,
                          checkpoint=lambda: manifest_guard.check(stage='manifest_validation'))
        manifest_guard.finish('manifest_validation_resource_samples.json')
    except Exception as exc:
        result = {
            'schema_version': 2, 'benchmark_status': 'FAILED',
            'stage': certification_stage,
            'development_override': certification_stage is None,
            'rows_per_source_basis': manifest['rows_per_source_basis'],
            'scenario': manifest['scenario'], 'format': manifest['format'],
            'elapsed_seconds': round(perf_counter() - started, 3), 'run_directory': None,
            'settings': vars(settings), 'stage_seconds': {}, 'resource_summary': {},
            'verification': {'passed': False, 'errors': [str(exc)]},
            'exports': [], 'python': platform.python_version(), 'duckdb': duckdb.__version__,
            'platform': platform.platform(),
            'claim': 'This result applies only to the recorded stage, scenario, format, settings, and outputs.',
        }
        _atomic_json(Path(root) / 'benchmark_result.json', result)
        _write_evidence_manifest(root, result)
        raise SystemExit('Benchmark verification failed; inspect benchmark_result.json.') from exc
    directory = execute(
        manifest['files']['legacy'], manifest['files']['saas'], root=Path(root) / 'runs', settings=settings,
        traffic_date='2026-10-25', full_audit=manifest['scenario'] == 'high_cardinality_mismatch',
        cancel_event=cancel_event,
    )
    exports = []
    verification = {'passed': False, 'errors': ['Benchmark verification did not run.']}
    run_meta = {}
    with Run(directory) as run:
        export_monitor = None
        try:
            if run.meta['status'] != 'COMPLETED':
                verification = {'passed': False, 'errors': [run.meta.get('error', run.meta['status'])]}
            else:
                export_monitor = ResourceMonitor(run, cancel_event, append=True).start()
                run.current_stage = 'benchmark_verification'
                verification = verify_run(run, manifest['expected'], manifest)
                export_monitor.checkpoint()
                run.meta['benchmark_export_status'] = ('PENDING' if verification['passed'] and complete_exports
                                                       else 'NOT_REQUESTED')
                run.save()
        except Exception as exc:
            verification['passed'] = False
            verification.setdefault('errors', []).append(str(exc))
            run.meta['benchmark_export_status'] = 'FAILED'
            run.save()
        finally:
            if export_monitor is not None:
                export_monitor.finish()
            run_meta = dict(run.meta)
    if verification['passed'] and complete_exports:
        try:
            exports = _run_export_worker(
                root, directory,
                monotonic() + max(0.0, settings.max_elapsed_seconds - (perf_counter() - started)),
                cancel_event)
            with Run(directory) as run:
                run.meta['benchmark_export_status'] = 'COMPLETED'
                run.save()
                run_meta = dict(run.meta)
        except Exception as exc:
            verification['passed'] = False
            verification.setdefault('errors', []).append(str(exc))
            with Run(directory) as run:
                run.meta['benchmark_export_status'] = ('TIMED_OUT' if 'ELAPSED_TIME_LIMIT' in str(exc) else 'FAILED')
                run.save()
                run_meta = dict(run.meta)
    result = {
            'schema_version': 2, 'benchmark_status': 'PASSED' if verification['passed'] else 'FAILED',
            'stage': stage if manifest['rows_per_source_basis'] == STAGES[stage] else None,
            'development_override': manifest['rows_per_source_basis'] != STAGES[stage],
            'rows_per_source_basis': manifest['rows_per_source_basis'],
            'scenario': manifest['scenario'], 'format': manifest['format'],
            'elapsed_seconds': round(perf_counter() - started, 3), 'run_directory': str(directory),
            'settings': vars(settings), 'stage_seconds': run_meta.get('stage_seconds', {}),
            'resource_summary': run_meta.get('resource_summary', {}), 'verification': verification,
            'exports': exports, 'python': platform.python_version(), 'duckdb': duckdb.__version__,
            'platform': platform.platform(),
            'claim': 'This result applies only to the recorded stage, scenario, format, settings, and outputs.',
        }
    _atomic_json(Path(root) / 'benchmark_result.json', result)
    _write_evidence_manifest(root, result)
    if not verification['passed']:
        raise SystemExit('Benchmark verification failed; inspect benchmark_result.json.')
    return result


def _require_fresh_root(root, mode):
    root = Path(root)
    if mode in ('generate', 'both'):
        if root.exists() and any(root.iterdir()):
            raise ValueError(f'Benchmark directory must be new or empty; refusing to overwrite: {root}')
        root.mkdir(parents=True, exist_ok=True)
    else:
        if not (root / 'fixture_manifest.json').is_file():
            raise ValueError(f'Run mode requires fixture_manifest.json: {root}')
        if (root / 'benchmark_result.json').exists() or (root / 'runs').exists():
            raise ValueError(f'Run mode refuses a directory containing prior run evidence: {root}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage', type=int, choices=STAGES)
    parser.add_argument('--rows', type=int, help='Explicit sub-Stage-1 development override')
    parser.add_argument('--scenario', choices=SCENARIOS + tuple(SCENARIO_ALIASES), default='identical')
    parser.add_argument('--format', choices=('csv', 'xlsx', 'cross'), default='csv')
    parser.add_argument('--mode', choices=('generate', 'run', 'both'), default='both')
    parser.add_argument('--directory', required=True)
    parser.add_argument('--memory', default='4GB')
    parser.add_argument('--threads', type=int, default=4)
    parser.add_argument('--rows-per-file', type=int, default=1_000_000)
    parser.add_argument('--temp-directory')
    parser.add_argument('--monitor-interval', type=float, default=2.0)
    parser.add_argument('--free-space-reserve-gb', type=int, default=50)
    parser.add_argument('--process-memory-limit-gb', type=int, default=14)
    parser.add_argument('--available-memory-floor-gb', type=int, default=4)
    parser.add_argument('--max-elapsed-seconds', type=float)
    parser.add_argument('--skip-complete-exports', action='store_true')
    args = parser.parse_args()
    if args.stage is None and args.rows is None:
        parser.error('Specify --stage or an explicit --rows development override.')
    if args.rows is not None and (args.rows < 1 or args.rows >= STAGES[1]):
        parser.error('--rows is restricted to positive sub-Stage-1 development workloads.')
    if args.stage is not None and args.rows is not None and args.rows != STAGES[args.stage]:
        parser.error('--rows cannot override an approved stage size.')
    if args.format in ('xlsx', 'cross') and args.rows_per_file > 1_000_000:
        parser.error('XLSX basis rows per file cannot exceed 1,000,000.')
    stage = args.stage or 1
    rows = args.rows or STAGES[stage]
    root = Path(args.directory).resolve()
    _require_fresh_root(root, args.mode)
    cancel_event = Event()
    previous_handler = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, lambda *_: cancel_event.set())
    try:
        settings = Settings(
            memory_limit=args.memory, threads=args.threads, max_temp_directory_size=TEMP_CAPS[stage],
            temp_directory=args.temp_directory, monitor_interval_seconds=args.monitor_interval,
            max_elapsed_seconds=args.max_elapsed_seconds or TIME_CAPS[stage],
            free_space_reserve_gb=args.free_space_reserve_gb,
            process_memory_limit_gb=args.process_memory_limit_gb,
            available_memory_floor_gb=args.available_memory_floor_gb,
        )
        if args.mode == 'both':
            command = [
                sys.executable, '-m', 'tools.phase3a_benchmark', '--mode', 'generate',
                '--directory', str(root), '--scenario', _scenario(args.scenario),
                '--format', args.format, '--rows-per-file', str(args.rows_per_file),
                '--free-space-reserve-gb', str(args.free_space_reserve_gb),
                '--process-memory-limit-gb', str(args.process_memory_limit_gb),
                '--available-memory-floor-gb', str(args.available_memory_floor_gb),
                '--monitor-interval', str(args.monitor_interval),
                '--max-elapsed-seconds', str(args.max_elapsed_seconds or TIME_CAPS[stage]),
            ]
            command.extend(['--stage', str(args.stage)] if args.stage is not None else ['--rows', str(rows)])
            subprocess.run(command, check=True)
        if args.mode in ('generate', 'both'):
            if args.mode == 'generate':
                guard = GenerationGuard(root, settings, cancel_event, max(REQUIRED_FREE_GB[stage], args.free_space_reserve_gb))
                guard.check(force=True)
                manifest = generate(root, rows, args.scenario, args.format, args.rows_per_file, guard)
                guard.finish()
            else:
                manifest = json.loads((root / 'fixture_manifest.json').read_text(encoding='utf-8'))
        else:
            manifest = json.loads((root / 'fixture_manifest.json').read_text(encoding='utf-8'))
        if args.mode == 'generate':
            validation_guard = GenerationGuard(
                root, settings, cancel_event, max(REQUIRED_FREE_GB[stage], args.free_space_reserve_gb))
            validation_guard.check(stage='manifest_validation', force=True)
            validate_manifest(manifest, stage=args.stage, root=root,
                              checkpoint=lambda: validation_guard.check(stage='manifest_validation'))
            validation_guard.finish('manifest_validation_resource_samples.json')
        if args.mode in ('run', 'both'):
            result = run_benchmark(
                root, manifest, stage, args.memory, args.threads, not args.skip_complete_exports,
                temp_directory=args.temp_directory, monitor_interval=args.monitor_interval,
                cancel_event=cancel_event, max_elapsed_seconds=args.max_elapsed_seconds,
                free_space_reserve_gb=args.free_space_reserve_gb,
                process_memory_limit_gb=args.process_memory_limit_gb,
                available_memory_floor_gb=args.available_memory_floor_gb,
            )
            print(json.dumps(result, indent=2))
    except BaseException as exc:
        if not (root / 'benchmark_result.json').exists() and root.exists():
            _atomic_json(root / 'benchmark_failure.json', {
                'schema_version': 1, 'benchmark_status': 'CANCELLED' if cancel_event.is_set() else 'FAILED',
                'error': str(exc), 'stage': args.stage, 'rows_per_source_basis': rows,
                'scenario': _scenario(args.scenario), 'format': args.format,
            })
        raise
    finally:
        signal.signal(signal.SIGINT, previous_handler)


if __name__ == '__main__':
    main()
