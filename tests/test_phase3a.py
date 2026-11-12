import hashlib
import json
import sys
from time import monotonic
from decimal import Decimal
from threading import Event

import pytest

from core.engine import Run
from core.monitoring import GIB, ResourceMonitor
from core.schema import Settings
from core.workflow import execute
from tools.generate_samples import cdr, write_csv
import tools.phase3a_benchmark as benchmark
from tools.phase3a_benchmark import expected, generate, run_benchmark, validate_manifest, verify_run


def test_settings_validate_resource_guards():
    settings = Settings(memory_limit='4GB', max_temp_directory_size='20GB', free_space_reserve_gb=50)
    assert settings.process_memory_limit_gb == 14
    with pytest.raises(ValueError, match='temporary'):
        Settings(max_temp_directory_size='unlimited')
    with pytest.raises(ValueError, match='reserve'):
        Settings(free_space_reserve_gb=0)
    with pytest.raises(ValueError, match='elapsed'):
        Settings(max_elapsed_seconds=0)


def test_resource_monitor_limit_reasons_include_time_memory_and_disk(tmp_path):
    class Connection:
        def interrupt(self):
            pass

    class FakeRun:
        directory = tmp_path
        temp_directory = tmp_path
        current_stage = 'test'
        con = Connection()
        meta = {}
        settings = Settings(free_space_reserve_gb=50, process_memory_limit_gb=14,
                            available_memory_floor_gb=4, max_elapsed_seconds=30)

    monitor = ResourceMonitor(FakeRun())
    safe = {'working_set_bytes': GIB, 'available_memory_bytes': 8 * GIB,
            'disk_free_bytes': 100 * GIB, 'elapsed_seconds': 1}
    assert monitor._limit_reason(safe) is None
    assert monitor._limit_reason({**safe, 'working_set_bytes': 15 * GIB}) == 'PROCESS_MEMORY_LIMIT'
    assert monitor._limit_reason({**safe, 'available_memory_bytes': 3 * GIB}) == 'AVAILABLE_MEMORY_FLOOR'
    assert monitor._limit_reason({**safe, 'disk_free_bytes': 49 * GIB}) == 'FREE_SPACE_RESERVE'
    assert monitor._limit_reason({**safe, 'elapsed_seconds': 31}) == 'ELAPSED_TIME_LIMIT'


def test_sha256_is_verified_and_reported(tmp_path):
    left = write_csv(tmp_path / 'legacy.csv', [cdr()])
    right = write_csv(tmp_path / 'saas.csv', [cdr()])
    directory = execute([left], [right], root=tmp_path / 'runs',
                        settings=Settings(memory_limit='256MB', free_space_reserve_gb=1,
                                          process_memory_limit_gb=20, available_memory_floor_gb=1))
    with Run(directory) as run:
        assert run.meta['status'] == 'COMPLETED', run.meta
        for record in run.meta['files']:
            assert record['sha256_verified'] is True
            assert record['sha256_before'] == record['sha256_after']
            assert record['sha256_before'] == hashlib.sha256(open(record['path'], 'rb').read()).hexdigest()
        assert run.meta['resource_summary']['sample_count'] >= 2
        assert (run.directory / 'resource_samples.jsonl').exists()


def test_phase3a_mixed_fixture_oracle_and_runner(tmp_path):
    manifest = generate(tmp_path / 'benchmark', 1010, 'mixed', 'csv', 500)
    assert manifest['expected'] == expected(1010, 'mixed')
    directory = execute(manifest['files']['legacy'], manifest['files']['saas'], root=tmp_path / 'runs',
                        settings=Settings(memory_limit='256MB', max_temp_directory_size='1GB',
                                          free_space_reserve_gb=1, process_memory_limit_gb=20,
                                          available_memory_floor_gb=1))
    with Run(directory) as run:
        verification = verify_run(run, manifest['expected'])
        assert verification['passed'], verification
        assert json.loads((tmp_path / 'benchmark' / 'fixture_manifest.json').read_text())['schema_version'] == 2


def test_prestart_cancellation_never_publishes_result(tmp_path):
    left = write_csv(tmp_path / 'legacy.csv', [cdr()])
    right = write_csv(tmp_path / 'saas.csv', [cdr()])
    cancelled = Event()
    cancelled.set()
    directory = execute([left], [right], root=tmp_path / 'runs', cancel_event=cancelled,
                        settings=Settings(free_space_reserve_gb=1, available_memory_floor_gb=1))
    with Run(directory) as run:
        assert run.meta['status'] == 'CANCELLED'
        assert 'result' not in run.meta
        assert run.meta['resource_summary']['stop_reason'] == 'USER_CANCELLED'


@pytest.mark.parametrize('scenario,counts,fields', [
    ('identical', (10, 0, 0, 0, 0), {}),
    ('reordered', (10, 0, 0, 0, 0), {}),
    ('rating_difference', (9, 1, 0, 0, 0), {'RATED_AMOUNT_TAP:NORMALIZED_VALUE': 1}),
    ('missing_additional', (8, 0, 1, 1, 0), {}),
    ('duplicate_ambiguous', (8, 0, 0, 0, 2), {}),
    ('mixed', (5, 1, 1, 1, 2), {'RATED_AMOUNT_TAP:NORMALIZED_VALUE': 1, 'RATE:NORMALIZED_VALUE': 1}),
    ('high_cardinality_mismatch', (10, 0, 0, 0, 0), {'RATE:NORMALIZED_VALUE': 10}),
])
def test_all_synthetic_scenario_oracles(scenario, counts, fields):
    oracle = expected(10, scenario)
    assert tuple(oracle[name] for name in (
        'MATCHED_SAME_RATING', 'MATCHED_DIFFERENT_RATING', 'LEGACY_ONLY',
        'SAAS_ONLY', 'DUPLICATE_OR_AMBIGUOUS')) == counts
    assert oracle['field_difference_counts'] == fields
    assert Decimal(oracle['financial']['variance']) == (
        Decimal('0.5') if scenario in ('rating_difference', 'mixed') else Decimal('0'))


def test_high_cardinality_fixture_values_are_unique_and_all_different():
    legacy = list(benchmark.benchmark_rows(20, 'legacy', 'high_cardinality_mismatch'))
    saas = list(benchmark.benchmark_rows(20, 'saas', 'high_cardinality_mismatch'))
    legacy_rates = [row['RATE'] for row in legacy]
    saas_rates = [row['RATE'] for row in saas]
    assert len(set(saas_rates)) == 20
    assert all(left != right for left, right in zip(legacy_rates, saas_rates))


def test_fixture_manifest_binds_hash_size_rows_and_scenario(tmp_path):
    root = tmp_path / 'fixture'
    manifest = generate(root, 20, 'mixed', 'csv', 10)
    validate_manifest(manifest, root=root)
    assert all(item['sha256'] and item['bytes'] > 0 for values in manifest['file_integrity'].values() for item in values)
    with open(manifest['files']['legacy'][0], 'ab') as handle:
        handle.write(b'changed')
    with pytest.raises(ValueError, match='SHA-256|byte size'):
        validate_manifest(manifest, root=root)


def test_benchmark_refuses_existing_evidence_and_stage_mismatch(tmp_path):
    occupied = tmp_path / 'occupied'
    occupied.mkdir()
    (occupied / 'historical.json').write_text('{}', encoding='utf-8')
    with pytest.raises(ValueError, match='refusing to overwrite'):
        benchmark._require_fresh_root(occupied, 'both')

    fixture = tmp_path / 'fixture'
    manifest = generate(fixture, 20, 'identical', 'csv', 20)
    with pytest.raises(ValueError, match='requires 2000000'):
        validate_manifest(manifest, stage=1, root=fixture)


def test_oracle_detects_exact_financial_tampering(tmp_path):
    manifest = generate(tmp_path / 'fixture', 20, 'rating_difference', 'csv', 20)
    directory = execute(
        manifest['files']['legacy'], manifest['files']['saas'], root=tmp_path / 'runs',
        settings=Settings(memory_limit='256MB', free_space_reserve_gb=1,
                          process_memory_limit_gb=20, available_memory_floor_gb=1),
    )
    with Run(directory) as run:
        assert verify_run(run, manifest['expected'], manifest)['passed']
        run.con.execute("UPDATE revenue SET amount_variance=amount_variance+0.000000000001 WHERE CURRENCY='USD'")
        verification = verify_run(run, manifest['expected'], manifest)
        assert not verification['passed']
        assert any('amount_variance' in error for error in verification['errors'])


def test_failed_complete_export_writes_failed_not_passed_result(tmp_path, monkeypatch):
    root = tmp_path / 'benchmark'
    manifest = generate(root, 20, 'identical', 'csv', 20)

    def fail_export(*_args, **_kwargs):
        raise RuntimeError('forced export failure')

    monkeypatch.setattr(benchmark, '_run_export_worker', fail_export)
    with pytest.raises(SystemExit, match='verification failed'):
        run_benchmark(
            root, manifest, 1, '256MB', 2, True, free_space_reserve_gb=1,
            process_memory_limit_gb=20, available_memory_floor_gb=1,
        )
    result = json.loads((root / 'benchmark_result.json').read_text(encoding='utf-8'))
    assert result['benchmark_status'] == 'FAILED'
    assert any('forced export failure' in error for error in result['verification']['errors'])
    assert (root / 'evidence_manifest.json').is_file()


def test_cancelled_benchmark_never_passes(tmp_path):
    root = tmp_path / 'benchmark'
    manifest = generate(root, 20, 'identical', 'csv', 20)
    cancelled = Event()
    cancelled.set()
    with pytest.raises(SystemExit, match='verification failed'):
        run_benchmark(
            root, manifest, 1, '256MB', 2, False, cancel_event=cancelled,
            free_space_reserve_gb=1, process_memory_limit_gb=20, available_memory_floor_gb=1,
        )
    result = json.loads((root / 'benchmark_result.json').read_text(encoding='utf-8'))
    assert result['benchmark_status'] == 'FAILED'
    assert result['exports'] == []


def test_blocked_export_worker_is_hard_terminated_and_not_published(tmp_path):
    run_directory = tmp_path / 'run'
    run_directory.mkdir()
    command = [sys.executable, '-c', 'import time; time.sleep(60)']
    started = monotonic()
    with pytest.raises(RuntimeError, match='ELAPSED_TIME_LIMIT'):
        benchmark._run_export_worker(
            tmp_path, run_directory, monotonic() + 0.2, poll_seconds=0.02,
            termination_grace_seconds=0.1, worker_command=command)
    assert monotonic() - started < 5
    assert not (run_directory / 'reports' / 'completed_export_manifest.json').exists()
    incomplete = list((run_directory / 'export_staging').glob('*/incomplete_manifest.json'))
    assert len(incomplete) == 1
    assert json.loads(incomplete[0].read_text())['status'] == 'INCOMPLETE'


def test_publication_manifest_is_last_and_crash_does_not_claim_completion(tmp_path, monkeypatch):
    run_directory = tmp_path / 'run'
    staging = tmp_path / 'staging'
    staging.mkdir(parents=True)
    artifact = staging / 'one.xlsx'
    artifact.write_bytes(b'verified')
    item = {'filename': artifact.name, 'bytes': artifact.stat().st_size,
            'sha256': hashlib.sha256(artifact.read_bytes()).hexdigest()}

    def crash_before_manifest(*_args, **_kwargs):
        raise OSError('forced publication crash')

    monkeypatch.setattr(benchmark, '_atomic_json', crash_before_manifest)
    with pytest.raises(OSError, match='forced publication crash'):
        benchmark._publish_verified_exports(run_directory, staging, {'exports': [item]})
    assert not (run_directory / 'reports' / 'completed_export_manifest.json').exists()
