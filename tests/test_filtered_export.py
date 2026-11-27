"""Filtered exports read saved results; fixtures never touch production runs."""
import hashlib
import tracemalloc
from itertools import zip_longest

from openpyxl import load_workbook
import pytest

from core.engine import Run
import core.reporting as reporting
from core.schema import Settings
from tools.generate_samples import cdr


def workbook_records(path):
    workbook = load_workbook(path, read_only=True, data_only=False)
    try:
        records = []
        for sheet in workbook:
            rows = sheet.values
            headers = next(rows)
            records.extend(dict(zip_longest(headers, row)) for row in rows)
        return headers, records
    finally:
        workbook.close()


@pytest.fixture
def belbx_run(run_pair):
    rows = [cdr(HPMN='BELBX', IMSI=f'00101{i:010d}') for i in range(1111)]
    rows += [cdr(HPMN='BELBXA'), cdr(HPMN='OTHER')]
    return run_pair(legacy=rows, saas=rows)


def test_belbx_export_contains_all_1111_across_pages(belbx_run):
    run = belbx_run
    filters = {'HPMN': 'BELBX', 'VPMN': '', 'CURRENCY': '', 'status': '', 'profile': '', 'match_key': ''}
    first, total = run.table_page('cdr_details', filters=filters)
    assert len(first) == 100 and total == 1111
    expected_keys = []
    for offset in range(0, total, 100):
        page, count = run.table_page('cdr_details', filters=filters, offset=offset)
        assert count == total and len(page) <= 100
        expected_keys.extend(row['match_key'] for row in page)
    target, count, digest = reporting.export_filtered_records_xlsx(run, filters, expected_rows=total)
    headers, records = workbook_records(target)
    assert count == len(records) == 1111
    assert [row['match_key'] for row in records] == expected_keys
    assert {row['HPMN'] for row in records} == {'BELBX'}
    assert set(first[0]) <= set(headers)
    assert {'legacy_amount', 'saas_amount', 'rating_difference', 'status', 'HPMN', 'VPMN'} <= set(headers)
    assert records[0]['legacy_amount'] == '1.000000000001'
    assert records[0]['Rating Variance (%)'] == '0.000000000000'
    assert target.parent == run.directory / 'reports'
    assert digest == hashlib.sha256(target.read_bytes()).hexdigest()
    assert not (target.parent / 'CDR_Matching_Details_All_Records.xlsx').exists()


@pytest.mark.parametrize('selection', [
    {'HPMN': 'BELBX'}, {'VPMN': 'TST91'}, {'CURRENCY': 'USD'},
    {'status': 'MATCHED_DIFFERENT_RATING'}, {'profile': 'GPRS'},
    {'HPMN': 'BELBX', 'VPMN': 'TST91', 'CURRENCY': 'USD',
     'status': 'MATCHED_DIFFERENT_RATING', 'profile': 'GPRS'},
    {'HPMN': 'BELBX', 'VPMN': 'TST91', 'CURRENCY': 'USD',
     'status': 'MATCHED_DIFFERENT_RATING', 'profile': 'GPRS', 'exact_key': True},
    {'HPMN': "BELBX' OR 1=1 --"}, {'status': None},
])
def test_export_matches_table_filters(run_pair, selection):
    rows = [cdr(HPMN='BELBX', IMSI='001'),
            cdr(HPMN='OTHER', IMSI='002'),
            cdr(HPMN='BELBX', VPMN='TST92', IMSI='003'),
            cdr(HPMN='BELBX', CURRENCY='EUR', IMSI='004'),
            cdr(HPMN='BELBX', SERVICE_TYPE='SMS-MO', IMSI='005'),
            cdr(HPMN='BELBX', IMSI='006')]
    right = [dict(row, RATED_AMOUNT_TAP='2.000000000002') if i != 5 else row for i, row in enumerate(rows)]
    run = run_pair(legacy=rows, saas=right)
    filters = dict(selection)
    if filters.pop('exact_key', False):
        filters['match_key'] = run.table_page('cdr_details', filters=filters)[0][0]['match_key']
    expected, total = run.table_page('cdr_details', filters=filters)
    target, count, _ = reporting.export_filtered_records_xlsx(run, filters, expected_rows=total)
    _, records = workbook_records(target)
    assert count == total == len(records)
    assert {row['match_key'] for row in records} == {row['match_key'] for row in expected}
    for record in records:
        assert record['legacy_amount'] == '1.000000000001'
        if record['status'] == 'MATCHED_DIFFERENT_RATING':
            assert record['saas_amount'] == '2.000000000002'
            assert record['rating_difference'] == '1.000000000001'
            assert record['Rating Variance (%)'] == '100.000000000000'


def test_reporting_percentage_handles_repeating_zero_missing_and_different_currencies(run_pair):
    left = [cdr(HPMN='BELBX', IMSI=str(i), RATED_AMOUNT_TAP=value)
            for i, value in enumerate(('3', '0', '1', '1', '7', '-2'))]
    right = [dict(row, RATED_AMOUNT_TAP=value) for row, value in zip(left, ('4', '1', '0', '1', '7', '-1'))]
    right[3]['CURRENCY'] = 'EUR'
    right.pop(4)
    right.append(cdr(HPMN='BELBX', IMSI='saas-only'))
    run = run_pair(legacy=left, saas=right)
    path, count, _ = reporting.export_filtered_records_xlsx(run, {'HPMN': 'BELBX'})
    _, records = workbook_records(path)
    percentages = {row['IMSI']: row['Rating Variance (%)'] for row in records}
    assert count == 7
    assert percentages == {'0': '33.333333333333', '1': None, '2': '-100.000000000000',
                           '3': None, '4': None, '5': '-50.000000000000', 'saas-only': None}


def test_filtered_export_retains_scale_24_amounts_and_percentage(run_pair):
    run = run_pair(legacy=[cdr(RATED_AMOUNT_TAP='0.000000000000000000000003')],
                   saas=[cdr(RATED_AMOUNT_TAP='0.000000000000000000000004')],
                   settings=Settings(memory_limit='256MB', threads=1, decimal_scale=24))
    path, count, _ = reporting.export_filtered_records_xlsx(run)
    _, records = workbook_records(path)
    assert count == 1
    assert records[0]['legacy_amount'] == '0.000000000000000000000003'
    assert records[0]['saas_amount'] == '0.000000000000000000000004'
    assert records[0]['rating_difference'] == '0.000000000000000000000001'
    assert records[0]['Rating Variance (%)'] == '33.333333333333333333333333'


def test_empty_results_write_header_only(belbx_run):
    path, count, _ = reporting.export_filtered_records_xlsx(belbx_run, {'HPMN': 'NO_MATCH'}, expected_rows=0)
    headers, records = workbook_records(path)
    assert count == 0 and records == []
    assert 'legacy_amount' in headers


def test_null_profile_filter_matches_table(run_pair):
    rows = [cdr(SERVICE_TYPE=''), cdr(IMSI='normal')]
    run = run_pair(legacy=rows, saas=rows)
    filters = {'profile': None}
    expected, total = run.table_page('cdr_details', filters=filters)
    path, count, _ = reporting.export_filtered_records_xlsx(run, filters, expected_rows=total)
    _, records = workbook_records(path)
    assert count == total == 1
    assert records[0]['profile'] is None
    assert records[0]['match_key'] == expected[0]['match_key']


def test_filtered_export_preserves_existing_all_records_files_and_schema(run_pair):
    rows = [cdr(HPMN='BELBX'), cdr(HPMN='OTHER')]
    run = run_pair(legacy=rows, saas=rows)
    all_paths = [reporting.export_all_records(run, format)[0] for format in ('csv', 'xlsx')]
    before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in all_paths}
    path, count, _ = reporting.export_filtered_records_xlsx(run, {'HPMN': 'BELBX'}, expected_rows=1)
    assert count == 1 and path not in all_paths
    assert {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in all_paths} == before
    headers, records = workbook_records(all_paths[1])
    assert len(records) == 2 and 'Rating Variance (%)' not in headers


@pytest.mark.parametrize('count, sheet_sizes', [(0, [0]), (3, [3]), (4, [3, 1]), (7, [3, 3, 1])])
def test_filtered_excel_limits_include_repeated_headers(run_pair, monkeypatch, count, sheet_sizes):
    rows = [cdr(IMSI=str(i), HPMN='BELBX') for i in range(max(1, count))]
    run = run_pair(legacy=rows, saas=rows)
    monkeypatch.setattr(reporting, 'EXCEL_MAX_ROWS', 4)
    path, exported, _ = reporting.export_filtered_records_xlsx(run, {'HPMN': 'BELBX' if count else 'NONE'})
    workbook = load_workbook(path, read_only=True)
    try:
        sizes = []
        for sheet in workbook:
            values = sheet.values
            assert next(values)[0] == 'match_key'
            sizes.append(sum(1 for _ in values))
        assert exported == count and sizes == sheet_sizes
    finally:
        workbook.close()


def historical_fixture(tmp_path, rows=2):
    """A saved older reporting schema, with no current ingestion/matching tables."""
    with Run.create(tmp_path / 'historical', Settings(memory_limit='128MB', threads=1)) as run:
        run.con.execute('''CREATE TABLE legacy AS SELECT i AS record_id,
            '000' || CAST(i AS VARCHAR) AS IMSI, '001' AS ANUM, '002' AS BNUM,
            '000003' AS GPRS_CHARGING_ID, 0::DECIMAL(38,12) AS TAX_AMOUNT_TAP,
            0::DECIMAL(38,12) AS FINAL_TAX_AMOUNT, 0.0001::DECIMAL(38,12) AS RATE
            FROM range(?) r(i)''', [rows])
        run.con.execute('CREATE TABLE saas AS SELECT * FROM legacy')
        run.con.execute('''CREATE VIEW cdr_details AS SELECT 'GPRS:' || IMSI AS match_key,
            'GPRS' AS profile, 'MATCHED_SAME_RATING' AS status, record_id AS legacy_id,
            record_id AS saas_id, 'BELBX' AS HPMN, 'TST91' AS VPMN, 'USD' AS CURRENCY,
            12345678901234567890.123456789012::DECIMAL(38,12) AS legacy_amount,
            12345678901234567890.123456789012::DECIMAL(38,12) AS saas_amount,
            0::DECIMAL(38,12) AS rating_difference,
            12.345678901234::DECIMAL(38,12) AS "Rating Variance (%)" FROM legacy''')
        run.meta.update(status='COMPLETED', phase=2,
                        settings={'memory_limit': '128MB', 'threads': 1, 'decimal_scale': 12})
        run.save()
        return run.directory


def test_historical_completed_run_and_columns_remain_unchanged(tmp_path, monkeypatch):
    directory = historical_fixture(tmp_path)
    protected = [directory / 'run.json', directory / 'reconciliation.duckdb']
    before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in protected}

    def forbid_save(_run):
        raise AssertionError('Filtered export must never save historical run metadata')

    monkeypatch.setattr(Run, 'save', forbid_save)
    with Run(directory) as run:
        columns = [row[0] for row in run.con.execute('DESCRIBE cdr_details').fetchall()]
        path, count, _ = reporting.export_filtered_records_xlsx(run, {'HPMN': 'BELBX'}, expected_rows=2)
    headers, records = workbook_records(path)
    assert count == 2 and list(headers[:len(columns)]) == columns
    assert records[0]['legacy_amount'] == '12345678901234567890.123456789012'
    assert records[0]['Rating Variance (%)'] == '12.345678901234'
    assert {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in protected} == before


def test_large_export_keeps_python_memory_and_cursor_batches_bounded(tmp_path):
    directory = historical_fixture(tmp_path, rows=12001)
    batches = []

    class BoundedCursor:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, query, parameters=None):
            self.query = query
            self.connection.execute(query, parameters or [])
            return self

        @property
        def description(self):
            return self.connection.description

        def fetchall(self):
            assert self.query.startswith('DESCRIBE'), 'Record data must never use fetchall()'
            return self.connection.fetchall()

        def fetchone(self):
            return self.connection.fetchone()

        def fetchmany(self, size):
            assert size <= 2000
            batch = self.connection.fetchmany(size)
            batches.append(len(batch))
            return batch

    with Run(directory) as run:
        connection = run.con
        run.con = BoundedCursor(connection)
        tracemalloc.start()
        try:
            path, count, _ = reporting.export_filtered_records_xlsx(run, {'HPMN': 'BELBX'}, expected_rows=12001)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
            run.con = connection
    assert count == 12001 and path.is_file()
    assert sum(batches) == count and max(batches) == 2000
    assert peak < 32 * 1024 * 1024  # Python only; DuckDB retains its configured memory budget.


def test_stale_count_and_invalid_filters_do_not_publish(run_pair):
    run = run_pair()
    with pytest.raises(ValueError, match='count changed'):
        reporting.export_filtered_records_xlsx(run, expected_rows=100)
    with pytest.raises(ValueError, match='Unknown filter'):
        reporting.export_filtered_records_xlsx(run, {'not_a_column': 'x'})
    assert not list((run.directory / 'reports').glob('CDR_Matching_Details_Filtered*'))


def test_filtered_export_rejects_incomplete_run(run_pair):
    run = run_pair()
    run.meta['status'] = 'CANCELLED'
    with pytest.raises(ValueError, match='completed run'):
        reporting.export_filtered_records_xlsx(run)
    assert not list((run.directory / 'reports').glob('CDR_Matching_Details_Filtered*'))


def test_interrupted_filtered_export_does_not_publish(run_pair):
    run = run_pair()

    def interrupt(event):
        if event['event'] == 'all_records_batch':
            raise InterruptedError('cancel export')

    with pytest.raises(InterruptedError, match='cancel export'):
        reporting.export_filtered_records_xlsx(run, checkpoint=interrupt)
    assert not list((run.directory / 'reports').glob('CDR_Matching_Details_Filtered*'))
