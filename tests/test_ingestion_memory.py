"""Streaming ingestion keeps exact values, physical provenance, and file atomicity."""
import csv
from decimal import Decimal

import pytest

from core.engine import Run
from core.ingestion import ingest
from core.monitoring import ResourceMonitor
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
