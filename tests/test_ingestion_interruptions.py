"""Operational stops must escape CSV validation and stop the whole run."""
from threading import Event, Timer

import duckdb
import pytest

from core.engine import Run
from core.monitoring import ResourceMonitor
from core.schema import Settings
from core.workflow import execute
from tools.generate_samples import cdr, write_csv
import core.ingestion as ingestion


@pytest.mark.parametrize('reason', ['AVAILABLE_MEMORY_FLOOR', 'USER_CANCELLED', 'UNATTRIBUTED'])
def test_real_duckdb_interrupt_is_fatal_and_not_invalid_csv(tmp_path, monkeypatch, reason):
    left = write_csv(tmp_path / 'legacy.csv', [cdr()])
    right = write_csv(tmp_path / 'saas.csv', [cdr()])
    cancel = Event()
    original_csv = ingestion._csv
    original_sample = ResourceMonitor._sample
    original_monitor_run = ResourceMonitor._run
    query_started = Event()
    interrupted = Event()
    scanned = []

    def sample(monitor):
        values = original_sample(monitor)
        if reason == 'AVAILABLE_MEMORY_FLOOR' and query_started.is_set():
            values['available_memory_bytes'] = 0
        return values

    def monitor_run(monitor):
        if reason == 'UNATTRIBUTED':
            # An interrupt without an application stop reason must remain unknown.
            assert query_started.wait(5)
            if not monitor.stop_event.wait(0.1):
                monitor.run.con.interrupt()
            return
        else:
            original_monitor_run(monitor)

    def csv(run, source, path, options):
        scanned.append(source)
        if source == 'legacy':
            run.emit('csv_scan', source=source, source_file=str(path))
            query_started.set()
            if reason == 'USER_CANCELLED':
                cancel.set()
            try:
                # Bounded-memory, long-running query produces a real DuckDB
                # InterruptException without needing production CSVs in tests.
                run.con.execute('SELECT sum(i) FROM range(1000000000000) t(i)')
            except duckdb.InterruptException:
                interrupted.set()
                raise
            pytest.fail('Expected the active DuckDB query to be interrupted')
        return original_csv(run, source, path, options)

    monkeypatch.setattr(ResourceMonitor, '_sample', sample)
    monkeypatch.setattr(ResourceMonitor, '_run', monitor_run)
    monkeypatch.setattr(ingestion, '_csv', csv)
    directory = execute([left], [right], root=tmp_path / 'runs', cancel_event=cancel,
                        settings=Settings(memory_limit='256MB', threads=1,
                                          monitor_interval_seconds=0.5))
    assert interrupted.is_set(), 'Test must exercise a real engine interruption'
    with Run(directory) as run:
        expected = {'USER_CANCELLED': 'CANCELLED', 'AVAILABLE_MEMORY_FLOOR': 'ABORTED_RESOURCE_LIMIT',
                    'UNATTRIBUTED': 'FAILED'}[reason]
        assert run.meta['status'] == expected, run.meta
        assert 'result' not in run.meta
        assert scanned == ['legacy']
        assert len(run.meta['files']) == 1
        assert run.meta['files'][0]['status'] == 'INTERRUPTED'
        assert run.meta['files'][0]['error_type'] == 'InterruptException'
        assert not run.rows("SELECT * FROM issues WHERE code='FILE_ERROR'")
        assert run.rows("SELECT * FROM issues WHERE code='INGESTION_INTERRUPTED'")
        assert run.meta['resource_summary']['stop_reason'] == (None if reason == 'UNATTRIBUTED' else reason)
        if reason != 'UNATTRIBUTED':
            details = run.meta['resource_summary']['stop_details']
            assert details['reason'] == reason
            assert details['stage'] == 'csv_scan'
            if reason == 'AVAILABLE_MEMORY_FLOOR':
                assert details['sample']['available_memory_bytes'] == 0
        assert not run.rows("SELECT * FROM information_schema.tables WHERE table_name IN ('saas','raw_saas','revenue')")


def test_stop_during_python_provenance_aborts_before_scan(tmp_path, monkeypatch):
    left = write_csv(tmp_path / 'legacy.csv', [cdr()])
    right = write_csv(tmp_path / 'saas.csv', [cdr()])
    cancel = Event()

    def progress(event):
        if event['stage'] == 'csv_provenance':
            cancel.set()  # DuckDB is idle: interrupt() alone cannot stop Python.

    directory = execute([left], [right], root=tmp_path / 'runs', cancel_event=cancel,
                        progress=progress, settings=Settings(memory_limit='256MB'))
    with Run(directory) as run:
        assert run.meta['status'] == 'CANCELLED', run.meta
        assert 'result' not in run.meta
        assert run.meta['files'][0]['status'] == 'INTERRUPTED'
        assert len(run.meta['files']) == 1
        assert not run.rows("SELECT * FROM issues WHERE code='FILE_ERROR'")
        assert not (directory / 'csv_rows.csv').exists()


def test_hashing_honors_stop_checkpoint(tmp_path):
    path = tmp_path / 'blocks.bin'
    path.write_bytes(b'0123456789')
    checks = []

    def checkpoint():
        checks.append(True)
        if len(checks) == 2:
            from core.monitoring import OperationStopped
            raise OperationStopped('Stopped while hashing')

    with pytest.raises(RuntimeError, match='Stopped while hashing'):
        ingestion._sha256(path, block_size=4, checkpoint=checkpoint)
    assert len(checks) == 2


def test_late_cancel_never_publishes_completed_result(tmp_path):
    left = write_csv(tmp_path / 'legacy.csv', [cdr()])
    right = write_csv(tmp_path / 'saas.csv', [cdr()])
    cancel = Event()

    def progress(event):
        if event['stage'] == 'parquet_export':
            cancel.set()

    directory = execute([left], [right], root=tmp_path / 'runs', cancel_event=cancel,
                        progress=progress, settings=Settings(memory_limit='256MB'))
    with Run(directory) as run:
        assert run.meta['status'] == 'CANCELLED', run.meta
        assert 'result' not in run.meta
        assert 'aggregate_result' not in run.meta


def test_actual_csv_stage_interrupt_cleans_spools_and_retains_error(tmp_path, monkeypatch):
    left = write_csv(tmp_path / 'legacy.csv', [cdr()])
    right = write_csv(tmp_path / 'saas.csv', [cdr()])
    create = Run.create
    interrupted = Event()

    class SlowScan:
        def __init__(self, connection):
            self.connection = connection

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def execute(self, sql, params=None):
            if sql.startswith('INSERT INTO raw_legacy') and 'read_csv(' in sql:
                # Exercise _csv's real scan/finally path with a deliberately long
                # synthetic workload, interrupted on this run's connection only.
                sql = sql.replace('true AS __dpm_record_present',
                                  '(SELECT sum(i) FROM range(1000000000000) workload(i)) IS NOT NULL AS __dpm_record_present')
                timer = Timer(0.1, self.connection.interrupt)
                timer.start()
                try:
                    return self.connection.execute(sql)
                except duckdb.InterruptException:
                    interrupted.set()
                    raise
                finally:
                    timer.cancel()
                    timer.join()
            return self.connection.execute(sql, params or [])

    def create_run(*args, **kwargs):
        run = create(*args, **kwargs)
        run.con = SlowScan(run.con)
        return run

    monkeypatch.setattr(Run, 'create', create_run)
    directory = execute([left], [right], root=tmp_path / 'runs',
                        settings=Settings(memory_limit='256MB', threads=1))
    assert interrupted.is_set()
    with Run(directory) as run:
        assert run.meta['status'] == 'FAILED', run.meta
        assert run.meta['error_type'] == 'InterruptException'
        assert run.meta['files'][0]['status'] == 'INTERRUPTED'
        assert not run.rows("SELECT * FROM issues WHERE code='FILE_ERROR'")
        assert not run.rows("SELECT * FROM information_schema.tables WHERE table_name='stage'")
        assert run.con.execute('SELECT count(*) FROM raw_legacy').fetchone()[0] == 0
    assert not (directory / 'csv_rows.csv').exists()
