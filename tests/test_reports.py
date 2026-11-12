import hashlib

from openpyxl import load_workbook
import pytest

from core.engine import Run
from core.reporting import REPORTS, export_report, export_detail, export_all_records_xlsx
from tools.generate_samples import cdr


def test_all_five_reports_and_exact_decimal_text(run_pair):
    run = run_pair()
    for report in REPORTS:
        target = export_report(run, report)
        workbook = load_workbook(target, read_only=True, data_only=False)
        assert 'Run Metadata' in workbook.sheetnames
        metadata = dict(workbook['Run Metadata'].values)
        assert metadata['run_id'] == run.meta['run_id']
        assert metadata['completed_at']
        assert 'Source Files' in workbook.sheetnames
        assert len(list(workbook['Source Files'].values)) == 3
        if report == 'Executive Summary':
            rows = list(workbook['revenue'].values)
            record = dict(zip(rows[0], rows[1]))
            assert record['legacy_amount'] == '1.000000000001'
        workbook.close()


def test_validation_report_and_formula_safety(run_pair):
    run = run_pair(legacy=[cdr(RATED_AMOUNT_TAP='=1+1')])
    path = export_report(run, 'Data Ingestion Validation')
    workbook = load_workbook(path, read_only=True, data_only=False)
    cells = list(workbook['issues'].rows)
    index = [c.value for c in cells[0]].index('raw_value')
    assert cells[1][index].value == '=1+1'
    assert cells[1][index].data_type == 's'
    workbook.close()
    with pytest.raises(ValueError, match='completed'):
        export_report(run, 'Executive Summary')


def test_sql_filtering_sorting_and_pagination(run_pair):
    run = run_pair(legacy=[cdr(), cdr(HPMN='TST02', RATED_AMOUNT_TAP='5')], saas=[cdr()])
    rows, total = run.table_page('hpmn', filters={'status': 'MISMATCH'}, sort='amount_abs_variance', descending=True, limit=1)
    assert total == 1
    assert rows[0]['HPMN'] == 'TST02'
    rows, total = run.table_page('hpmn', filters={'HPMN': "TST01' OR 1=1 --"})
    assert total == 0
    with pytest.raises(ValueError):
        run.table_page('legacy; DROP TABLE hpmn')


def test_streaming_detail_exports_and_filter(run_pair):
    run = run_pair(saas=[cdr(RATED_AMOUNT_TAP='3')])
    csv = export_detail(run, 'rating_differences', filters={'HPMN': 'TST01'})
    assert csv.read_text().count('\n') == 2
    parquet = export_detail(run, 'field_differences', 'parquet')
    assert run.con.execute('SELECT count(*) FROM read_parquet(?)', [str(parquet)]).fetchone()[0] == 1
    with pytest.raises(ValueError):
        export_detail(run, 'raw_legacy')


def test_excel_extract_and_worksheet_bounds(run_pair, monkeypatch, tmp_path):
    from openpyxl import Workbook
    import core.reporting as reporting
    run = run_pair(legacy=[cdr(GPRS_CHARGING_ID=str(i)) for i in range(8)])
    monkeypatch.setattr(reporting, 'EXCEL_MAX_ROWS', 4)
    workbook = Workbook(write_only=True)
    reporting._query_sheet(workbook, run, 'legacy_only', limit=5)
    target = tmp_path / 'bounded.xlsx'
    workbook.save(target)
    saved = load_workbook(target, read_only=True)
    try:
        assert len(saved.sheetnames) == 2
        assert sum(len(list(sheet.values))-1 for sheet in saved) == 5
    finally:
        saved.close()



def test_saved_run_queries_use_bounded_worker_count(run_pair):
    run = run_pair()
    assert run.con.execute("SELECT current_setting('threads')").fetchone()[0] == 1
    assert run.con.execute("SELECT current_setting('preserve_insertion_order')").fetchone()[0] is False
    run.con.execute('SET threads=2')
    export_detail(run, 'cdr_details', 'parquet')
    assert run.con.execute("SELECT current_setting('threads')").fetchone()[0] == 2


def test_saved_run_connections_do_not_conflict_on_catalog_writes(run_pair):
    run = run_pair()
    with Run(run.directory) as first, Run(run.directory) as second:
        assert first.con.execute('SELECT count(*) FROM counts').fetchone()[0] == 3
        assert second.con.execute('SELECT count(*) FROM counts').fetchone()[0] == 3


def test_duplicate_report_progress_is_batched_and_ordered(run_pair):
    duplicate = cdr(IMSI='duplicate')
    run = run_pair(legacy=[duplicate, duplicate], saas=[duplicate, duplicate, duplicate])
    events = []
    path = export_report(run, 'Duplicate Ambiguous', checkpoint=events.append)
    names = [event['event'] for event in events]
    assert names[0] == 'report_start'
    assert 'table_batch' in names
    assert names[-1] == 'report_complete'
    workbook = load_workbook(path, read_only=True)
    try:
        rows = list(workbook['ambiguous_records'].values)
        headers = rows[0]
        source = headers.index('source')
        record_id = headers.index('record_id')
        ordering = [(row[source], row[record_id]) for row in rows[1:]]
        assert ordering == sorted(ordering)
        assert len(ordering) == 5
    finally:
        workbook.close()


def test_workbook_save_failure_never_promotes_partial(run_pair, monkeypatch, tmp_path):
    import core.reporting as reporting
    run = run_pair()

    def fail_save(_self, _path):
        raise OSError('forced workbook failure')

    monkeypatch.setattr(reporting.Workbook, 'save', fail_save)
    with pytest.raises(OSError, match='forced workbook failure'):
        export_report(run, 'Executive Summary', destination=tmp_path)
    assert not (tmp_path / 'Executive_Summary.xlsx').exists()
    assert not (tmp_path / 'Executive_Summary.tmp.xlsx').exists()


def test_all_records_xlsx_exports_1182_with_provenance_and_integrity(run_pair, tmp_path):
    rows = [cdr(IMSI=f'00101{i:010d}', GPRS_CHARGING_ID=f'000{i:012d}',
                ANUM=f'00A{i:06d}', BNUM=f'00B{i:06d}',
                RATED_AMOUNT_TAP='1.000000000001') for i in range(1182)]
    run = run_pair(legacy=rows, saas=rows)
    target, count, digest = export_all_records_xlsx(run, destination=tmp_path)
    assert count == 1182
    assert digest == hashlib.sha256(target.read_bytes()).hexdigest()

    workbook = load_workbook(target, read_only=False, data_only=False)
    try:
        sheet = workbook['CDR Matching Details']
        assert sheet.max_row == 1183
        headers = [cell.value for cell in sheet[1]]
        assert sheet.freeze_panes == 'A2'
        assert sheet.auto_filter.ref.endswith('1')
        assert sheet.column_dimensions['A'].width > 10
        first = dict(zip(headers, [cell.value for cell in sheet[2]]))
        assert first['legacy_amount'] == '1.000000000001'
        assert first['saas_amount'] == '1.000000000001'
        assert first['revenue_variance'] == '0.000000000000'
        assert first['legacy_row'] == 2
        assert first['saas_row'] == 2
        assert first['legacy_file'].endswith('.csv')
        assert first['saas_file'].endswith('.csv')
        for field in ('match_key', 'IMSI', 'ANUM', 'BNUM', 'GPRS_CHARGING_ID'):
            cell = sheet.cell(2, headers.index(field) + 1)
            assert cell.data_type == 's'
            assert str(cell.value).startswith('0') if field != 'match_key' else cell.value.startswith('GPRS:')
    finally:
        workbook.close()
