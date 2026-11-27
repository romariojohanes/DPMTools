from pathlib import Path
from threading import Event
from time import monotonic, sleep

from streamlit.testing.v1 import AppTest

from core.reporting import REPORTS
from core.engine import Run
from core.schema import FIELDS
from core.workflow import execute
from tools.generate_samples import cdr, write_csv


def finish_processing(app):
    deadline = monotonic() + 60
    while 'job_future' in app.session_state and monotonic() < deadline:
        sleep(0.1)
        app.run(timeout=30)
    assert 'job_future' not in app.session_state
    return app


def finish_detail_export(app):
    deadline = monotonic() + 60
    while 'detail_export_future' in app.session_state and monotonic() < deadline:
        sleep(0.1)
        app.run(timeout=30)
    assert 'detail_export_future' not in app.session_state
    return app


def finish_filtered_export(app):
    deadline = monotonic() + 60
    while 'filtered_export_future' in app.session_state and monotonic() < deadline:
        sleep(0.1)
        app.run(timeout=30)
    assert 'filtered_export_future' not in app.session_state
    assert 'filtered_export_error' not in app.session_state
    assert not app.exception
    return app


def test_filtered_excel_download_1111_rows_and_stale_selection(run_pair, tmp_path, monkeypatch):
    from openpyxl import load_workbook
    rows = [cdr(HPMN='BELBX', IMSI=f'00101{i:010d}') for i in range(1111)] + [cdr(HPMN='BELBXA')]
    run = run_pair(legacy=rows, saas=rows)
    monkeypatch.setenv('DPM_RUN_ROOT', str(tmp_path / 'runs'))
    app = AppTest.from_file(str(Path(__file__).parents[1] / 'app.py')).run(timeout=30)
    app.sidebar.radio[0].set_value('CDR Matching Summary').run()
    app.text_input(key='cdr_details_HPMN').set_value(' belbx ').run()
    app.number_input(key='cdr_details_number').set_value(2).run()
    assert any('1,111 matching rows' in caption.value for caption in app.caption)
    assert len(app.dataframe[-1].value) == 100
    app.button(key='prepare_filtered_records').click().run()
    finish_filtered_export(app)
    result = app.session_state['filtered_export_result']
    assert result['rows'] == 1111 and result['filters']['HPMN'] == 'BELBX'
    assert Path(result['path']).parent == run.directory / 'reports'
    assert any(button.label == 'Download Filtered Records (Excel)' for button in app.get('download_button'))
    workbook = load_workbook(result['path'], read_only=True)
    try:
        assert sum(1 for _ in workbook.active.values) == 1112
    finally:
        workbook.close()
    app.number_input(key='cdr_details_number').set_value(3).run()
    assert any(button.label == 'Download Filtered Records (Excel)' for button in app.get('download_button'))
    app.text_input(key='cdr_details_VPMN').set_value('NO_MATCH').run()
    assert not any(button.label == 'Download Filtered Records (Excel)' for button in app.get('download_button'))
    assert app.button(key='prepare_filtered_records')
    # The existing export controls and format choices remain available.
    assert any(button.label == 'Export All Records' for button in app.button)
    assert app.radio(key='cdr_all_records_format').options == ['CSV (recommended)', 'XLSX']


def test_filtered_export_large_file_uses_local_path_and_hides_other_runs(run_pair, tmp_path, monkeypatch):
    import core.reporting as reporting
    first = run_pair(legacy=[cdr(HPMN='BELBX')], saas=[cdr(HPMN='BELBX')])
    second = run_pair(legacy=[cdr(HPMN='BELBX', IMSI='other')], saas=[cdr(HPMN='BELBX', IMSI='other')])
    monkeypatch.setenv('DPM_RUN_ROOT', str(tmp_path / 'runs'))
    monkeypatch.setattr(reporting, 'FILTERED_DOWNLOAD_LIMIT_BYTES', 1)
    app = AppTest.from_file(str(Path(__file__).parents[1] / 'app.py')).run(timeout=30)
    app.sidebar.selectbox[0].set_value(first.directory.name).run()
    app.sidebar.radio[0].set_value('CDR Matching Summary').run()
    app.button(key='prepare_filtered_records').click().run()
    finish_filtered_export(app)
    result = app.session_state['filtered_export_result']
    assert any('verified local file' in item.value for item in app.info)
    assert any(item.value == result['path'] for item in app.code)
    assert not any(button.label == 'Download Filtered Records (Excel)' for button in app.get('download_button'))
    original_open = Path.open

    def guard_browser_read(path, mode='r', *args, **kwargs):
        assert not (str(path) == result['path'] and mode == 'rb'), 'Oversized export was read into browser memory'
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, 'open', guard_browser_read)
    app.run()
    assert not app.exception
    app.sidebar.selectbox[0].set_value(second.directory.name).run()
    assert app.button(key='prepare_filtered_records')
    assert not any(item.value == result['path'] for item in app.code)


def test_initial_app_renders(tmp_path, monkeypatch):
    monkeypatch.setenv('DPM_RUN_ROOT', str(tmp_path / 'runs'))
    app = AppTest.from_file(str(Path(__file__).parents[1] / 'app.py')).run(timeout=30)
    assert not app.exception
    assert app.title[0].value == 'DPM+ Roaming Reconciliation Tool'
    assert any(button.label == 'Start reconciliation' for button in app.button)
    assert '%m/%d/%y' in app.selectbox(key='legacy_date_format').options
    assert '%m/%d/%y' in app.selectbox(key='saas_date_format').options


def test_successful_run_and_all_pages(tmp_path, monkeypatch):
    monkeypatch.setenv('DPM_RUN_ROOT', str(tmp_path / 'runs'))
    app = AppTest.from_file(str(Path(__file__).parents[1] / 'app.py')).run(timeout=30)
    app.button[0].click().run()
    assert app.number_input(key='normalization_chunk_rows').value == 250_000
    app.number_input(key='normalization_chunk_rows').set_value(10_000)
    next(b for b in app.button if b.label == 'Start reconciliation').click().run(timeout=60)
    finish_processing(app)
    assert not app.exception
    assert app.success
    with Run(next((tmp_path / 'runs').iterdir())) as saved_run:
        assert saved_run.settings.normalization_chunk_rows == 10_000
    assert len(app.metric) == 3
    for page in ['HPMN Comparison', 'VPMN Comparison', 'HPMN/VPMN Drill-down', 'CDR Matching Summary',
                 'Rating Differences', 'Legacy Only', 'SaaS Only', 'Duplicate / Ambiguous Keys',
                 'Field-Level Differences', 'Revenue Variance Attribution', 'Export Reports', 'New Reconciliation']:
        app.sidebar.radio[0].set_value(page).run()
        assert not app.exception, page
        if page == 'Export Reports':
            for report in REPORTS:
                app.button(key=report).click().run()
                assert not app.exception, report
                assert any(item.label == 'Download ' + report for item in app.get('download_button'))
            next(item for item in app.selectbox if item.label == 'Detail table').set_value('cdr_details').run()
            next(item for item in app.selectbox if item.label == 'Detail format').set_value('parquet').run()
            next(button for button in app.button if button.label == 'Stream complete detail to disk').click().run()
            finish_detail_export(app)
            assert app.session_state['detail_export_result']['format'] == 'parquet'
            assert any(item.label == 'Download verified PARQUET' for item in app.get('download_button'))


def test_validation_failure_and_report_page(tmp_path, monkeypatch):
    monkeypatch.setenv('DPM_RUN_ROOT', str(tmp_path / 'runs'))
    root = Path(__file__).parents[1]
    app = AppTest.from_file(str(root / 'app.py')).run(timeout=30)
    app.text_area(key='legacy_paths').set_value(str(root / 'samples' / 'invalid' / 'legacy.csv'))
    app.text_area(key='saas_paths').set_value(str(root / 'samples' / 'invalid' / 'saas.csv'))
    app.button(key='start_reconciliation').click().run(timeout=60)
    finish_processing(app)
    assert not app.exception
    assert any('VALIDATION_FAILED' in error.value for error in app.error)
    assert int(app.metric[0].value) >= 1
    app.sidebar.radio[0].set_value('Export Reports').run()
    assert not app.exception
    assert app.button(key='Executive Summary').disabled
    app.button(key='Data Ingestion Validation').click().run()
    assert not app.exception


def test_background_input_error_remains_visible(tmp_path, monkeypatch):
    monkeypatch.setenv('DPM_RUN_ROOT', str(tmp_path / 'runs'))
    app = AppTest.from_file(str(Path(__file__).parents[1] / 'app.py')).run(timeout=30)
    app.text_area(key='legacy_paths').set_value(str(tmp_path / 'missing.csv'))
    app.text_area(key='saas_paths').set_value(str(tmp_path / 'other.csv'))
    app.button(key='start_reconciliation').click().run(timeout=30)
    finish_processing(app)
    assert not app.exception
    assert any('Input does not exist' in error.value for error in app.error)


def test_cancelled_run_does_not_instruct_user_to_correct_csv(tmp_path, monkeypatch):
    root = tmp_path / 'runs'
    left = write_csv(tmp_path / 'legacy.csv', [cdr()])
    right = write_csv(tmp_path / 'saas.csv', [cdr()])
    cancelled = Event()
    cancelled.set()
    directory = execute([left], [right], root=root, cancel_event=cancelled)
    monkeypatch.setenv('DPM_RUN_ROOT', str(root))
    app = AppTest.from_file(str(Path(__file__).parents[1] / 'app.py')).run(timeout=30)
    app.sidebar.radio[0].set_value('Executive Summary').run()
    assert not app.exception
    assert any('CANCELLED' in error.value for error in app.error)
    assert any('USER_CANCELLED' in message.value for message in app.info)
    assert not any('correct the source files' in message.value for message in app.info)
    with Run(directory) as run:
        assert 'result' not in run.meta


def test_user_configurable_legacy_and_saas_mapping(tmp_path, monkeypatch):
    monkeypatch.setenv('DPM_RUN_ROOT', str(tmp_path / 'runs'))
    row = cdr()
    legacy_headers = {field: f'Legacy {field.lower()}' for field in FIELDS}
    saas_headers = {field: f'SaaS {field.lower()}' for field in FIELDS}
    left = write_csv(tmp_path / 'legacy_mapped.csv', [
        {legacy_headers[field]: row[field] for field in FIELDS}], fields=list(legacy_headers.values()))
    right = write_csv(tmp_path / 'saas_mapped.csv', [
        {saas_headers[field]: row[field] for field in FIELDS}], fields=list(saas_headers.values()))
    app = AppTest.from_file(str(Path(__file__).parents[1] / 'app.py')).run(timeout=30)
    app.text_area(key='legacy_paths').set_value(str(left))
    app.text_area(key='saas_paths').set_value(str(right))
    app.button(key='legacy_inspect_columns').click().run(timeout=30)
    app.button(key='saas_inspect_columns').click().run(timeout=30)
    for field in FIELDS:
        app.selectbox(key=f'legacy_map_{field}').set_value('LEGACY_' + field)
        app.selectbox(key=f'saas_map_{field}').set_value('SAAS_' + field)
    app.button(key='start_reconciliation').click().run(timeout=60)
    finish_processing(app)
    assert not app.exception
    assert app.success
