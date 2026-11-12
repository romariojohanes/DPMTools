from pathlib import Path
from time import monotonic, sleep

from streamlit.testing.v1 import AppTest

from core.reporting import REPORTS
from core.schema import FIELDS
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


def test_initial_app_renders(tmp_path, monkeypatch):
    monkeypatch.setenv('DPM_RUN_ROOT', str(tmp_path / 'runs'))
    app = AppTest.from_file(str(Path(__file__).parents[1] / 'app.py')).run(timeout=30)
    assert not app.exception
    assert app.title[0].value == 'DPM+ Roaming Reconciliation Tool'
    assert any(button.label == 'Start reconciliation' for button in app.button)


def test_successful_run_and_all_pages(tmp_path, monkeypatch):
    monkeypatch.setenv('DPM_RUN_ROOT', str(tmp_path / 'runs'))
    app = AppTest.from_file(str(Path(__file__).parents[1] / 'app.py')).run(timeout=30)
    app.button[0].click().run()
    next(b for b in app.button if b.label == 'Start reconciliation').click().run(timeout=60)
    finish_processing(app)
    assert not app.exception
    assert app.success
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
