"""Streaming ingestion keeps exact values, physical provenance, and file atomicity."""
import csv
from decimal import Decimal

import pytest

from core.engine import Run
from core.ingestion import ingest
from core.monitoring import OperationStopped, ResourceMonitor
from core.schema import FIELDS, InputOptions, Settings
from core.workflow import execute
from tools.generate_samples import cdr, write_csv


@pytest.mark.parametrize('line_ending', ['\n', '\r\n'], ids=['LF', 'CRLF'])
def test_streamed_provenance_across_vectors_files_and_normalization_chunks(tmp_path, line_ending):
    first = tmp_path / 'first.csv'
    expected = []
    physical_line = 2
    count = 20_513
    with first.open('w', encoding='utf-8-sig', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS + ['UNUSED_INPUT'], lineterminator=line_ending)
        writer.writeheader()
        for i in range(count):
            if i % 5000 == 0:
                handle.write(line_ending)
                physical_line += 1
            service = 'DATA' + line_ending + 'MULTILINE' if i % 4096 == 0 else 'DATA'
            row = cdr(GPRS_CHARGING_ID=str(i), SERVICE=service,
                      RATED_AMOUNT_TAP='0.000000000000000000000001')
            row['UNUSED_INPUT'] = 'ignored'
            writer.writerow(row)
            expected.append((str(i), physical_line))
            physical_line += 1 + service.count('\n')
    second = write_csv(tmp_path / 'second.csv', [cdr(GPRS_CHARGING_ID='last',
                                                   RATED_AMOUNT_TAP='0.000000000000000000000001')])
    with Run.create(tmp_path / 'runs', Settings(memory_limit='256MB', threads=2, decimal_scale=24,
                                              normalization_chunk_rows=10_000)) as run:
        progress_rows = []

        def progress(event):
            if event['stage'] == 'normalization' and event.get('records'):
                # Progress callbacks cannot retain an already-finished wide chunk.
                assert not run.con.execute("SELECT table_name FROM duckdb_tables() WHERE table_name='cleaned'").fetchall()
                progress_rows.append(event['records'])

        run.progress = progress
        ingest(run, 'legacy', [first, second], InputOptions())
        assert run.issue_count() == 0
        assert run.con.execute('SELECT GPRS_CHARGING_ID, source_row FROM raw_legacy WHERE source_file=? ORDER BY source_row',
                               [str(first)]).fetchall() == expected
        assert run.con.execute('SELECT GPRS_CHARGING_ID, source_row FROM legacy WHERE source_file=?',
                               [str(second)]).fetchall() == [('last', 2)]
        assert run.con.execute('SELECT sum(RATED_AMOUNT_TAP) FROM legacy').fetchone()[0] == Decimal('0.000000000000000000020514')
        assert progress_rows == [10_000, 20_000, 20_514]
        assert run.meta['decimal_requirements']['legacy']['observed_fractional_digits'] == 24
        assert all(record['sha256_verified'] for record in run.meta['files'])
    assert not (run.directory / 'csv_rows.csv').exists()


@pytest.mark.parametrize('mismatch', ['shorter', 'longer'])
def test_provenance_mismatch_rolls_back_all_streamed_rows(tmp_path, mismatch):
    left = write_csv(tmp_path / 'legacy.csv', (cdr(GPRS_CHARGING_ID=str(i)) for i in range(3000)))
    right = write_csv(tmp_path / 'saas.csv', [cdr()])
    root = tmp_path / 'runs'

    def progress(event):
        if event['stage'] == 'csv_scan' and event['source'] == 'legacy' and 'records' not in event:
            index = next(root.glob('*/csv_rows.csv'))
            lines = index.read_text(encoding='ascii').splitlines()
            lines = lines[:-1] if mismatch == 'shorter' else lines + ['3001,3002']
            index.write_text('\n'.join(lines) + '\n', encoding='ascii')

    directory = execute([left], [right], root=root, progress=progress,
                        settings=Settings(memory_limit='256MB', threads=2))
    with Run(directory) as run:
        assert run.meta['status'] == 'VALIDATION_FAILED', run.meta
        assert run.con.execute('SELECT count(*) FROM raw_legacy').fetchone()[0] == 0
        assert run.rows("SELECT message FROM issues WHERE code='FILE_ERROR' AND message LIKE '%row counts disagree%'")
        assert not run.rows("SELECT * FROM issues WHERE code='INGESTION_INTERRUPTED'")


def test_resource_samples_identify_source_and_current_progress(tmp_path):
    with Run.create(tmp_path / 'runs', Settings(memory_limit='256MB')) as run:
        monitor = ResourceMonitor(run)
        run.emit('normalization', source='legacy', records=250_000)
        first = monitor._sample()
        assert (first['stage'], first['source'], first['records']) == ('normalization', 'legacy', 250_000)
        run.emit('csv_scan', source='saas', source_file='synthetic.csv')
        second = monitor._sample()
        assert second['source'] == 'saas'
        assert second['source_file'] == 'synthetic.csv'
        assert 'records' not in second


def test_csv_uses_positional_scan_and_preserves_null_records_with_filename_headers(tmp_path):
    fields = FIELDS + ['filename', '__dpm_csv_file']
    values = [dict(cdr(GPRS_CHARGING_ID='first'), filename='', __dpm_csv_file=''),
              dict.fromkeys(fields, ''),
              dict(cdr(GPRS_CHARGING_ID='last'), filename='input-data', __dpm_csv_file='input-data')]
    path = write_csv(tmp_path / 'source.csv', values, fields=fields)
    plans = []

    class PlannedConnection:
        def __init__(self, con):
            self.con = con

        def __getattr__(self, name):
            return getattr(self.con, name)

        def execute(self, sql, params=None):
            if sql.startswith('INSERT INTO raw_legacy') and 'read_csv(' in sql:
                plans.append(self.con.execute('EXPLAIN ' + sql).fetchone()[1])
            return self.con.execute(sql, params or [])

    with Run.create(tmp_path / 'runs', Settings(memory_limit='256MB', decimal_scale=24)) as run:
        run.con = PlannedConnection(run.con)
        ingest(run, 'legacy', [path], InputOptions())
        # Pinned DuckDB 1.4.1 must select the streaming operator, not the general
        # positional join that retains an input. This guards the memory fix.
        assert len(plans) == 1
        assert 'POSITIONAL_SCAN' in plans[0]
        assert 'POSITIONAL_JOIN' not in plans[0]
        assert run.meta['files'][0]['status'] == 'READ'
        assert run.meta['files'][0]['rows'] == 3
        assert run.meta['files'][0]['sha256_verified']
        assert run.con.execute('SELECT GPRS_CHARGING_ID,source_row FROM raw_legacy ORDER BY ingest_id').fetchall() == [
            ('first', 2), (None, 3), ('last', 4)]
        assert run.rows("SELECT * FROM issues WHERE source_row=3 AND code='REQUIRED_VALUE'")
        assert not run.rows("SELECT * FROM issues WHERE code='FILE_ERROR'")


@pytest.mark.parametrize('memory', ['256MB', '1GB', '2000 MB'])
def test_csv_budget_respects_lower_limits_and_restores_normalization_budget(tmp_path, memory):
    path = write_csv(tmp_path / 'source.csv', [cdr()])
    with Run.create(tmp_path / 'runs', Settings(memory_limit=memory)) as run:
        before = run.con.execute("SELECT current_setting('memory_limit')").fetchone()[0]
        scan_settings, normalization_settings = [], []

        def progress(event):
            current = run.con.execute("SELECT current_setting('memory_limit')").fetchone()[0]
            if event['stage'] == 'csv_scan':
                scan_settings.append((event['memory_limit'], current))
            elif event['stage'] == 'normalization':
                normalization_settings.append(current)

        run.progress = progress
        ingest(run, 'legacy', [path], InputOptions())
        expected = memory if memory != '2000 MB' else '1GB'
        assert scan_settings and all(item[0] == expected for item in scan_settings)
        assert all(current == before for current in normalization_settings)
        assert run.con.execute("SELECT current_setting('memory_limit')").fetchone()[0] == before
        assert run.meta['files'][0]['csv_memory_limit'] == expected
        assert run.meta['files'][0]['rows'] == 1
        assert run.issue_count() == 0
        assert (scan_settings[0][1] == before) == (memory != '2000 MB')


def test_csv_budget_restored_after_interruption_before_insert(tmp_path):
    path = write_csv(tmp_path / 'source.csv', [cdr()])
    with Run.create(tmp_path / 'runs', Settings(memory_limit='4GB')) as run:
        before = run.con.execute("SELECT current_setting('memory_limit')").fetchone()[0]

        def stop(event):
            if event['stage'] == 'csv_scan':
                assert event['memory_limit'] == '1GB'
                assert run.con.execute("SELECT current_setting('memory_limit')").fetchone()[0] != before
                raise OperationStopped('Synthetic cancellation')

        run.progress = stop
        with pytest.raises(OperationStopped, match='Synthetic cancellation'):
            ingest(run, 'legacy', [path], InputOptions())
        assert run.con.execute("SELECT current_setting('memory_limit')").fetchone()[0] == before
        assert run.meta['files'][0]['status'] == 'INTERRUPTED'
        assert run.con.execute('SELECT count(*) FROM raw_legacy').fetchone()[0] == 0
        assert not (run.directory / 'csv_rows.csv').exists()


def test_lenovo_available_memory_stop_remains_enforced(tmp_path):
    with Run.create(tmp_path / 'runs', Settings(memory_limit='4GB')) as run:
        monitor = ResourceMonitor(run)
        sample = dict(working_set_bytes=3_287_031_808, available_memory_bytes=4_231_184_384,
                      disk_free_bytes=100 * 1024**3, elapsed_seconds=200.4)
        assert run.settings.available_memory_floor_gb == 4
        assert monitor._limit_reason(sample) == 'AVAILABLE_MEMORY_FLOOR'
        assert monitor._limit_reason({**sample, 'available_memory_bytes': 4 * 1024**3}) is None
