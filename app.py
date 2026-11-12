"""DPM+ Roaming Reconciliation Tool v1.0 — managed local Windows interface."""
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from queue import Queue, Empty, Full
from threading import Event
from time import perf_counter
from datetime import date
from decimal import Decimal
from pathlib import Path

import streamlit as st

from core.engine import Run
from core.ingestion import inspect_headers, validate_header_mapping
from core.reporting import REPORTS, DETAIL_TABLES, export_report, export_detail, export_all_records_xlsx
from core.schema import FIELDS, InputOptions, Settings, MATCH_PROFILES, canonical, resolve_inputs
from core.workflow import execute
from core.summary_revenue import (SUMMARY_FIELDS, SummaryInputOptions, execute_summary,
                                  export_summary_xlsx, inspect_summary_headers,
                                  validate_summary_header_mapping)

ROOT = Path(__file__).resolve().parent
RUNS = Path(os.environ.get('DPM_RUN_ROOT', str(ROOT / 'data' / 'runs'))).resolve()
st.set_page_config(page_title='DPM+ | Roaming Reconciliation', page_icon='🔎', layout='wide')
st.markdown('''<style>
.block-container {padding-top:2rem; max-width:1500px}
[data-testid="stSidebar"] {background-color:#eef3f8;border-right:1px solid #dce3eb}
[data-testid="stMetric"] {border:1px solid #dce3eb;border-radius:10px;padding:16px}
.eyebrow {font-size:12px;letter-spacing:2px;color:#54718e;font-weight:700}
</style>''', unsafe_allow_html=True)
st.markdown('<div class="eyebrow">DPM+ v1.0 · LOCAL RECONCILIATION</div>', unsafe_allow_html=True)
st.title('DPM+ Roaming Reconciliation Tool')
st.caption('CSG Roaming Legacy (On-Premises) ↔ CSG Roaming SaaS · Exact financial and CDR reconciliation')


def display_rows(rows):
    return [{key: format(value, 'f') if isinstance(value, Decimal) else value for key, value in row.items()} for row in rows]


def catalog():
    result = {}
    if RUNS.exists():
        for meta_file in sorted(RUNS.glob('*/run.json'), reverse=True):
            try:
                meta = json.loads(meta_file.read_text(encoding='utf-8'))
                result[meta['run_id']] = (meta_file.parent, meta)
            except (OSError, ValueError, KeyError):
                continue
    return result


def request_start():
    st.session_state['start_requested'] = True


AUTOMATIC_MAPPING = '(automatic)'


def selected_mapping(state, source, fields=FIELDS):
    selected = [state.get(f'{source}_map_{field}', AUTOMATIC_MAPPING) for field in fields]
    explicit = [(value, field) for value, field in zip(selected, fields) if value != AUTOMATIC_MAPPING]
    sources = [value for value, _ in explicit]
    if len(sources) != len(set(sources)):
        raise ValueError(f'{source.title()} column mapping assigns one source column more than once.')
    return dict(explicit) or None


if 'pending_navigation' in st.session_state:
    st.session_state.update(st.session_state.pop('pending_navigation'))
if 'job_error' in st.session_state:
    st.error(st.session_state.pop('job_error'))


@st.cache_resource
def processing_pool():
    return ThreadPoolExecutor(max_workers=1, thread_name_prefix='dpm-local')


def detail_export_worker(directory, table_name, output_format):
    with Run(directory) as export_run:
        if table_name == 'summary_comparison' and output_format == 'xlsx':
            path, rows, digest = export_summary_xlsx(export_run)
            return {'path': str(path), 'table': table_name, 'format': output_format,
                    'bytes': path.stat().st_size, 'rows': rows, 'sha256': digest}
        if table_name == 'cdr_details' and output_format == 'xlsx':
            path, rows, digest = export_all_records_xlsx(export_run)
            return {'path': str(path), 'table': table_name, 'format': output_format,
                    'bytes': path.stat().st_size, 'rows': rows, 'sha256': digest}
        path = export_detail(export_run, table_name, output_format)
        return {'path': str(path), 'table': table_name, 'format': output_format,
                'bytes': path.stat().st_size}


def request_detail_export(directory, table_name, output_format):
    st.session_state.pop('detail_export_result', None)
    st.session_state['detail_export_request'] = (str(directory), table_name, output_format)


if 'detail_export_request' in st.session_state and 'detail_export_future' not in st.session_state:
    request = st.session_state.pop('detail_export_request')
    st.session_state['detail_export_future'] = processing_pool().submit(detail_export_worker, *request)


if st.session_state.pop('start_requested', False):
    try:
        state = st.session_state
        summary_mode = state.get('reconciliation_type', 'CDR Reconciliation') == 'Summary Revenue Reconciliation'
        fields = SUMMARY_FIELDS if summary_mode else FIELDS
        options = {}
        for source in ('legacy', 'saas'):
            common = dict(delimiter=state[source + '_delimiter'], encoding=state[source + '_encoding'],
                          decimal_separator=state[source + '_decimal'], sheet_name=state[source + '_sheet'].strip() or None,
                          column_mapping=selected_mapping(state, source, fields))
            options[source] = (SummaryInputOptions(**common) if summary_mode else
                               InputOptions(date_format=state[source + '_date_format'], **common))
            selected_paths = resolve_inputs(
                [line.strip() for line in state[source + '_paths'].splitlines() if line.strip()])
            if summary_mode:
                validate_summary_header_mapping(inspect_summary_headers(selected_paths, options[source]), options[source])
            else:
                validate_header_mapping(inspect_headers(selected_paths, options[source]), options[source])
        events = Queue(maxsize=1)
        def latest_progress(event):
            try:
                events.put_nowait(event)
            except Full:
                try:
                    events.get_nowait()
                except Empty:
                    pass
                events.put_nowait(event)
        state.pop('last_progress', None)
        state['job_events'] = events
        state['job_cancel'] = Event()
        state['job_started'] = perf_counter()
        worker = execute_summary if summary_mode else execute
        worker_kwargs = dict(
            legacy_options=options['legacy'], saas_options=options['saas'],
            settings=Settings(memory_limit=state['memory_limit'], temp_directory=state['temp_directory'].strip() or None,
                              decimal_scale=int(state['decimal_scale']), threads=int(state['threads']),
                              max_temp_directory_size=state['max_temp_directory_size'],
                              free_space_reserve_gb=int(state['free_space_reserve_gb']),
                              process_memory_limit_gb=int(state['process_memory_limit_gb']),
                              available_memory_floor_gb=int(state['available_memory_floor_gb'])), root=RUNS,
            progress=latest_progress, cancel_event=state['job_cancel'])
        if not summary_mode:
            worker_kwargs.update(traffic_date=state['traffic_date'] if state['constrain_date'] else None,
                                 matching_profiles=json.loads(state['matching_profiles']), full_audit=state['full_audit'])
        state['job_future'] = processing_pool().submit(
            worker,
            [line.strip() for line in state['legacy_paths'].splitlines() if line.strip()],
            [line.strip() for line in state['saas_paths'].splitlines() if line.strip()],
            **worker_kwargs)
    except Exception as exc:
        st.error(str(exc))


pages = ['New Reconciliation', 'Executive Summary', 'PLMN Comparison', 'Summary Legacy Only', 'Summary SaaS Only', 'HPMN Comparison', 'VPMN Comparison', 'HPMN/VPMN Drill-down', 'CDR Matching Summary', 'Rating Differences', 'Legacy Only', 'SaaS Only', 'Duplicate / Ambiguous Keys', 'Field-Level Differences', 'Revenue Variance Attribution', 'Export Reports']
runs = catalog()
with st.sidebar:
    st.markdown('### DPM+\nRoaming assurance')
    page = st.radio('Workspace', pages, key='page')
    selected = st.selectbox('Reconciliation run', list(runs), key='run_select',
                            format_func=lambda key: f"{key} · {runs[key][1]['status']}") if runs else None
    st.caption('Local processing · No external AI · Aggregate reconciliation')


def browse(key, mode):
    try:
        result = subprocess.run([sys.executable, str(ROOT / 'tools' / 'pick_paths.py'), mode],
                                capture_output=True, text=True, check=True)
        paths = json.loads(result.stdout)
        if paths:
            existing = st.session_state.get(key, '').strip()
            st.session_state[key] = '\n'.join(([existing] if existing else []) + paths)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        st.warning(f'File dialog unavailable. Paste local file or folder paths below. {exc}')


def source_input(source, label, summary_mode=False):
    st.subheader(label)
    first, second = st.columns(2)
    if first.button('Browse files', key=source + '_files'):
        browse(source + '_paths', 'files')
    if second.button('Browse folder', key=source + '_folder'):
        browse(source + '_paths', 'directory')
    paths = st.text_area('Local CSV/XLSX files or folders — one path per line', key=source + '_paths', height=130,
                         placeholder='D:\\Roaming\\Legacy\\2026-10-25')
    with st.expander('Source format settings'):
        delimiter = st.selectbox('CSV delimiter', [',', ';', '\t', '|'], key=source + '_delimiter', format_func=lambda value: 'Tab' if value == '\t' else value)
        encoding = st.text_input('CSV encoding', 'utf-8-sig', key=source + '_encoding')
        decimal = st.selectbox('Decimal separator', ['.', ','], key=source + '_decimal')
        date_format = st.selectbox('EVENT_DATE format', ['%m/%d/%Y', '%Y-%m-%d', '%Y%m%d', '%d/%m/%Y'], key=source + '_date_format', disabled=summary_mode)
        sheet = st.text_input('XLSX worksheet (required for multiple sheets)', key=source + '_sheet')
    with st.expander('Column mapping'):
        st.caption('Automatic mapping accepts canonical names, case/separator variants, and underscore-less aliases. Inspect headers to map other names.')
        if summary_mode:
            st.caption('Required: PLMN and TOTAL_CHARGE_SDR. All other Summary Revenue fields are optional and are mapped independently for each source.')
        if st.button('Inspect source columns', key=source + '_inspect_columns'):
            try:
                current_options = (SummaryInputOptions(delimiter=delimiter, encoding=encoding, decimal_separator=decimal,
                                                       sheet_name=sheet.strip() or None) if summary_mode else
                                   InputOptions(delimiter=delimiter, encoding=encoding,
                                                decimal_separator=decimal, date_format=date_format,
                                                sheet_name=sheet.strip() or None))
                resolved = resolve_inputs([line.strip() for line in paths.splitlines() if line.strip()])
                st.session_state[source + '_inspected_headers'] = (inspect_summary_headers(resolved, current_options)
                                                                    if summary_mode else inspect_headers(resolved, current_options))
                st.session_state.pop(source + '_mapping_error', None)
            except Exception as exc:
                st.session_state[source + '_mapping_error'] = str(exc)
        if st.session_state.get(source + '_mapping_error'):
            st.error(st.session_state[source + '_mapping_error'])
        inspected = st.session_state.get(source + '_inspected_headers', [])
        available = sorted({name for item in inspected for name in item['normalized_headers']})
        if inspected:
            st.dataframe([{'file': Path(item['path']).name,
                           'normalized_columns': ', '.join(item['normalized_headers'])} for item in inspected],
                         hide_index=True, width='stretch')
        fields = SUMMARY_FIELDS if summary_mode else FIELDS
        aliases = {field.replace('_', ''): field for field in fields}
        for field in fields:
            options = [AUTOMATIC_MAPPING] + available
            key = f'{source}_map_{field}'
            if key not in st.session_state and available:
                detected = next((name for name in available
                                 if aliases.get(name.replace('_', ''), name) == field), None)
                if detected:
                    st.session_state[key] = AUTOMATIC_MAPPING
            st.selectbox(field, options, key=key,
                         help=f'Source column used for canonical {field}. Leave automatic for recognized names.')
    return [line.strip() for line in paths.splitlines() if line.strip()], dict(delimiter=delimiter, encoding=encoding, decimal_separator=decimal, date_format=date_format, sheet_name=sheet.strip() or None)


def table(run, name, key, operators=()):
    columns = [row[0] for row in run.con.execute(f'DESCRIBE {name}').fetchall()]
    filter_columns = list(operators) + (['CURRENCY'] if 'CURRENCY' in columns else []) + (['status'] if 'status' in columns else [])
    filter_columns += [c for c in ('PLMN', 'profile', 'difference_type', 'match_key') if c in columns]
    if name == 'field_differences':
        filter_columns.append('field')
    if name == 'issues':
        filter_columns += ['source', 'code', 'field']
    filters = {}
    controls = st.columns(len(filter_columns))
    for control, field in zip(controls, filter_columns):
        if field in ('status', 'CURRENCY', 'source', 'code', 'field', 'profile', 'difference_type'):
            values = [r[0] for r in run.con.execute(f'SELECT DISTINCT "{field}" FROM {name} ORDER BY 1 LIMIT 300').fetchall()]
            filters[field] = control.selectbox(field, [''] + values, key=f'{key}_{field}', format_func=lambda value: '(Null)' if value is None else value or 'All')
        else:
            value = control.text_input(field + ' exact filter', value=st.session_state.get('investigate_' + field.lower(), ''), key=f'{key}_{field}').strip()
            filters[field] = value.upper() if field in ('HPMN', 'VPMN', 'PLMN') else value
    first, second, third = st.columns([3, 1, 1])
    sort = first.selectbox('Sort by', columns, key=key + '_sort')
    descending = second.checkbox('Descending', key=key + '_desc')
    number = int(third.number_input('Page', min_value=1, value=1, step=1, key=key + '_number'))
    rows, total = run.table_page(name, filters=filters, sort=sort, descending=descending, limit=100, offset=(number - 1) * 100)
    st.caption(f'{total:,} matching rows · Page {number:,} · Up to 100 rows per page · Financial values shown as exact decimal text')
    if rows:
        if name in ('hpmn', 'vpmn', 'pairs'):
            event = st.dataframe(display_rows(rows), hide_index=True, width='stretch', on_select='rerun', selection_mode='single-row', key=key + '_select')
            if event.selection.rows and st.button('Investigate selected CDRs', key=key + '_investigate'):
                selected_row = rows[event.selection.rows[0]]
                for field in ('HPMN', 'VPMN'):
                    st.session_state['investigate_' + field.lower()] = selected_row.get(field, '')
                    st.session_state['cdr_details_' + field] = selected_row.get(field, '')
                st.session_state['pending_navigation'] = {'page': 'CDR Matching Summary'}
                st.rerun()
        else:
            st.dataframe(display_rows(rows), hide_index=True, width='stretch')
    else:
        st.info('No rows on this page. Adjust the filters or page number.')


if 'job_future' in st.session_state:
    @st.fragment(run_every=1)
    def processing_monitor():
        state = st.session_state
        future = state.get('job_future')
        if future is None:
            return
        try:
            while True:
                state['last_progress'] = state['job_events'].get_nowait()
        except Empty:
            pass
        event = state.get('last_progress', {})
        st.info('Processing stage: ' + event.get('stage', 'starting'))
        st.write(f"Elapsed: {perf_counter()-state['job_started']:.1f} seconds")
        if event.get('source_file'):
            st.caption('Current file: ' + event['source_file'])
        if 'records' in event:
            st.write(f"Records processed in current file: {event['records']:,} | Measured throughput: {event.get('records_per_second', 0):,.0f} records/second")
        if st.button('Cancel active reconciliation', key='cancel_active_reconciliation'):
            state['job_cancel'].set()
            st.warning('Cancellation requested. The active database operation is being interrupted safely.')
        st.caption('Stage durations are saved with the run. ETA is omitted when the total work cannot be measured reliably.')
        if future.done():
            del state['job_future']
            state.pop('job_cancel', None)
            try:
                directory = future.result()
                state['pending_navigation'] = {'run_select': directory.name, 'page': 'Executive Summary'}
            except Exception as exc:
                state['job_error'] = str(exc)
            st.rerun()
    processing_monitor()

if 'detail_export_future' in st.session_state:
    @st.fragment(run_every=1)
    def detail_export_monitor():
        future = st.session_state.get('detail_export_future')
        if future is None:
            return
        if future.done():
            del st.session_state['detail_export_future']
            try:
                result = future.result()
                st.session_state['detail_export_result'] = result
                st.session_state['detail_export_message'] = 'Saved and verified ' + result['path']
            except Exception as exc:
                st.session_state['detail_export_error'] = str(exc)
            st.rerun()
        else:
            st.info('Complete detail export is running in the background.')
    detail_export_monitor()

if page == 'New Reconciliation':
    st.header('New Reconciliation')
    reconciliation_type = st.selectbox('Reconciliation Type', ['CDR Reconciliation', 'Summary Revenue Reconciliation'],
                                       key='reconciliation_type')
    summary_mode = reconciliation_type == 'Summary Revenue Reconciliation'
    st.write('Choose stable local extracts for both systems. Folders include immediate CSV/XLSX files; processing uses local disk.')
    if not summary_mode and st.button('Use synthetic matching samples', key='use_samples'):
        st.session_state['legacy_paths'] = str(ROOT / 'samples' / 'matching' / 'legacy.csv')
        st.session_state['saas_paths'] = str(ROOT / 'samples' / 'matching' / 'saas.xlsx')
    left, right = st.columns(2, gap='large')
    with left:
        legacy_paths, legacy_options = source_input('legacy', 'Legacy · On-Premises', summary_mode)
    with right:
        saas_paths, saas_options = source_input('saas', 'SaaS · CSG Roaming', summary_mode)
    constrain = st.checkbox('Require all rows to match one traffic date', value=False, key='constrain_date', disabled=summary_mode)
    traffic = st.date_input('Traffic date', value=date(2026, 10, 25), disabled=summary_mode or not constrain, key='traffic_date')
    if not summary_mode:
        st.caption('Without a traffic-date constraint, actual date ranges are reported. Check that both extracts cover the same intended period.')
    with st.expander('Processing settings'):
        memory = st.text_input('DuckDB memory limit', '4GB', key='memory_limit')
        temporary = st.text_input('Temporary directory (blank uses the run directory)', key='temp_directory')
        max_temporary = st.text_input('Maximum DuckDB temporary storage', '20GB', key='max_temp_directory_size')
        scale = int(st.number_input('Exact DECIMAL fractional digits', min_value=0, max_value=30, value=12, key='decimal_scale'))
        threads = int(st.number_input('DuckDB threads', min_value=1, max_value=32, value=4, key='threads'))
        reserve = int(st.number_input('Minimum free disk space (GB)', min_value=1, value=50, key='free_space_reserve_gb'))
        process_limit = int(st.number_input('Process working-set stop limit (GB)', min_value=1, value=14, key='process_memory_limit_gb'))
        available_floor = int(st.number_input('Available system memory floor (GB)', min_value=1, value=4, key='available_memory_floor_gb'))
        st.caption('The DuckDB limit is not a whole-process limit. Resource stops retain diagnostics and never publish a partial result.')
    with st.expander('CDR matching profiles'):
        st.text_area('Keys by SERVICE_TYPE (JSON)', value=json.dumps(MATCH_PROFILES, indent=2), height=240, key='matching_profiles')
        st.checkbox('Full audit of all 24 fields', key='full_audit')
        st.caption('Not used by Summary Revenue Reconciliation.' if summary_mode else 'Profiles are candidate keys. Missing values, unknown services and duplicate keys remain unresolved. Rating fields cannot be keys.')
    st.button('Start reconciliation', type='primary', key='start_reconciliation', on_click=request_start, disabled='job_future' in st.session_state)
else:
    if not selected:
        st.info('Create a reconciliation run to view processed results.')
        st.stop()
    directory, meta = runs[selected]
    is_summary = meta.get('reconciliation_type') == 'SUMMARY_REVENUE'
    st.caption(f"Run {selected} · {'Summary Revenue' if is_summary else 'CDR'} · Processed {meta.get('completed_at', meta['created_at'])}")
    if meta['status'] in ('CREATED', 'INGESTING'):
        st.warning('This run is processing or was interrupted. Start a new run if processing was interrupted.')
        st.stop()
    with Run(directory) as run:
        if is_summary and meta['status'] == 'COMPLETED' and page == 'Executive Summary':
            st.header('Executive Summary · Summary Revenue')
            (st.success if meta['result'] == 'MATCH' else st.warning)(meta['result'] + ' · Operator-level reconciliation')
            counts = meta['operator_counts']
            cols = st.columns(4)
            cols[0].metric('Legacy operators', counts['legacy'])
            cols[1].metric('SaaS operators', counts['saas'])
            cols[2].metric('Matched PLMNs', counts['MATCH'])
            cols[3].metric('Other statuses', counts['MISMATCH'] + counts['LEGACY_ONLY'] + counts['SAAS_ONLY'])
            if meta.get('warning_count'):
                st.warning(f"{meta['warning_count']:,} informational validation warning(s); reconciliation was not blocked.")
            st.subheader('Totals and variance (SaaS − Legacy)')
            table(run, 'summary_totals', 'summary_totals')
            st.subheader('Source provenance')
            st.dataframe(meta['files'], hide_index=True, width='stretch')
        elif is_summary and meta['status'] == 'COMPLETED' and page in ('PLMN Comparison', 'Summary Legacy Only', 'Summary SaaS Only'):
            name = {'PLMN Comparison': 'summary_comparison', 'Summary Legacy Only': 'summary_legacy_only',
                    'Summary SaaS Only': 'summary_saas_only'}[page]
            st.header(page)
            if page == 'PLMN Comparison':
                st.button('Export All Records', type='primary', disabled='detail_export_future' in st.session_state,
                          on_click=request_detail_export, args=(directory, 'summary_comparison', 'xlsx'),
                          help='Exports the complete comparison, independent of UI pagination.')
                detail_result = st.session_state.get('detail_export_result')
                if detail_result and detail_result.get('table') == 'summary_comparison':
                    detail_path = Path(detail_result['path'])
                    if detail_path.exists():
                        st.caption(f"{detail_result['rows']:,} records · SHA-256 {detail_result['sha256']} · {detail_path}")
                        if detail_path.stat().st_size <= 25 * 1024 * 1024:
                            with detail_path.open('rb') as handle:
                                st.download_button('Download All Records XLSX', handle, file_name=detail_path.name,
                                                   key='summary_all_records_download', on_click='ignore')
                        else:
                            st.info('This export exceeds 25 MB. Open the verified file at the local path above.')
            table(run, name, name)
        elif is_summary and page == 'Export Reports':
            st.header('Export Reports · Summary Revenue')
            st.info('Use Export All Records on PLMN Comparison. It contains the Executive Summary and complete detail.')
        elif is_summary and meta['status'] == 'COMPLETED':
            st.info('This page applies to CDR reconciliation. Choose a Summary Revenue page.')
        elif page == 'Export Reports':
            st.header('Export Reports')
            st.write('Reports contain run metadata, traffic dates, processing timestamps, and source file provenance. Decimal amounts are exported as text to retain every digit.')
            benchmark_export_status = meta.get('benchmark_export_status')
            benchmark_exports_visible = benchmark_export_status in (None, 'COMPLETED', 'NOT_REQUESTED')
            if benchmark_export_status in ('PENDING', 'FAILED', 'TIMED_OUT'):
                st.warning(f'Benchmark export set is {benchmark_export_status}. Incomplete staged artifacts are not published.')
            if meta.get('phase') == 2:
                st.subheader('Complete detail exports')
                detail = st.selectbox('Detail table', sorted(DETAIL_TABLES))
                detail_format = st.selectbox('Detail format', ['csv', 'parquet'])
                detail_message = st.session_state.pop('detail_export_message', None)
                detail_error = st.session_state.pop('detail_export_error', None)
                if detail_message is not None:
                    st.success(detail_message)
                if detail_error is not None:
                    st.error(detail_error)
                detail_result = st.session_state.get('detail_export_result')
                if detail_result:
                    detail_path = Path(detail_result['path'])
                    if detail_path.exists():
                        st.caption(f"{detail_path} · {detail_result['bytes']:,} bytes")
                        if detail_path.stat().st_size <= 25 * 1024 * 1024:
                            with detail_path.open('rb') as handle:
                                st.download_button('Download verified ' + detail_result['format'].upper(), handle,
                                                   file_name=detail_path.name, key='detail_export_download',
                                                   on_click='ignore')
                        else:
                            st.info('This detail export exceeds 25 MB. Open the verified file at the local path above.')
                st.button('Stream complete detail to disk', disabled='detail_export_future' in st.session_state,
                          on_click=request_detail_export, args=(directory, detail, detail_format))
            st.caption('Excel reports contain bounded extracts (up to 50,000 rows per table). CSV/Parquet detail exports contain all rows.')
            for report in REPORTS:
                disabled = meta['status'] != 'COMPLETED' and report != 'Data Ingestion Validation'
                target = run.directory / 'reports' / (report.replace(' ', '_') + '.xlsx')
                if st.button('Generate ' + report, disabled=disabled, key=report):
                    with st.spinner('Writing report to disk…'):
                        try:
                            export_report(run, report)
                        except Exception as exc:
                            st.error(f'Report export failed: {exc}')
                if target.exists() and benchmark_exports_visible:
                    st.caption(str(target))
                    if target.stat().st_size <= 25 * 1024 * 1024:
                        with target.open('rb') as handle:
                            st.download_button('Download ' + report, handle, file_name=target.name, key=report + '_download', on_click='ignore')
                    else:
                        st.info('This report exceeds 25 MB. Open the generated file at the local path above.')
        elif meta['status'] != 'COMPLETED':
            st.error(f"Run status: {meta['status']}. Reconciliation is blocked until all input issues are corrected.")
            if meta.get('error'):
                st.error(meta['error'])
            for source, requirement in meta.get('decimal_requirements', {}).items():
                if requirement['observed_fractional_digits'] > requirement['configured_scale']:
                    st.info(f"{source}: values require {requirement['observed_fractional_digits']} fractional digits; the selected scale is {requirement['configured_scale']}. Select sufficient precision in Processing settings and create a new run. No amounts were rounded.")
            st.metric('Validation issues', meta.get('issue_count', 0))
            st.dataframe(meta['files'], hide_index=True, width='stretch')
            table(run, 'issues', 'invalid')
            st.info('Export the Data Ingestion Validation report from Export Reports, correct the source files, and create a new run.')
        elif page == 'Executive Summary':
            st.header('Executive Summary')
            if meta['result'] == 'MATCH':
                st.success('MATCH · All seven aggregate reconciliation levels agree.')
            else:
                st.warning('MISMATCH · Differences exist in one or more aggregate levels.')
            st.caption('Equal aggregate revenue does not establish individual CDR equality. Duplicate and incomplete matching keys remain unresolved.')
            counts = run.rows('SELECT * FROM counts ORDER BY metric')
            for col, row in zip(st.columns(3), counts):
                col.metric(row['metric'].replace('_', ' '), f"{row['legacy_value']:,} / {row['saas_value']:,}", help='Legacy / SaaS')
                col.caption(f"{row['status']} · Absolute variance {row['abs_variance']:,}")
            st.subheader('Level 1 · Rated revenue by currency')
            table(run, 'revenue', 'summary_revenue')
            st.subheader('Levels 2–4 · Counts and operator populations')
            st.dataframe(counts, hide_index=True, width='stretch')
            left, right = st.columns(2)
            for column, name in ((left, 'hpmn_set'), (right, 'vpmn_set')):
                with column:
                    mismatch = run.con.execute(f"SELECT count(*) FROM {name} WHERE status='MISMATCH'").fetchone()[0]
                    st.write(f"{name[:4].upper()} population: {'MISMATCH' if mismatch else 'MATCH'} · {mismatch:,} one-source operators")
                    table(run, name, name)
            with st.expander('Run dates and source files'):
                st.json(meta['date_ranges'])
                st.dataframe(meta['files'], hide_index=True, width='stretch')
            with st.expander('Processing stage durations'):
                st.json(meta.get('stage_seconds', {}))
        elif page in ('CDR Matching Summary', 'Rating Differences', 'Legacy Only', 'SaaS Only', 'Duplicate / Ambiguous Keys', 'Field-Level Differences', 'Revenue Variance Attribution'):
            if meta.get('phase') != 2:
                st.info('This historical run has aggregate results only. Create a new run to use Phase 2 matching.')
                st.stop()
            st.header(page)
            if page == 'Revenue Variance Attribution':
                st.caption('MATCH confirms the attribution identity per currency. Unresolved duplicate amounts remain explicitly attributed; it does not certify those CDRs as matched.')
            mapping = {'CDR Matching Summary': 'matching_summary', 'Rating Differences': 'rating_differences',
                       'Legacy Only': 'legacy_only', 'SaaS Only': 'saas_only', 'Duplicate / Ambiguous Keys': 'duplicate_keys',
                       'Field-Level Differences': 'field_differences', 'Revenue Variance Attribution': 'revenue_attribution'}
            name = mapping[page]
            columns = [r[0] for r in run.con.execute(f'DESCRIBE {name}').fetchall()]
            table(run, name, name, tuple(c for c in ('HPMN', 'VPMN') if c in columns))
            if page == 'CDR Matching Summary':
                st.subheader('CDR Matching Details')
                st.button('Export All Records', type='primary', disabled='detail_export_future' in st.session_state,
                          on_click=request_detail_export, args=(directory, 'cdr_details', 'xlsx'),
                          help='Streams every record in this run to Excel; the 100-row table pagination does not apply.')
                detail_result = st.session_state.get('detail_export_result')
                if detail_result and detail_result.get('format') == 'xlsx':
                    detail_path = Path(detail_result['path'])
                    if detail_path.exists():
                        st.caption(f"{detail_result['rows']:,} records · SHA-256 {detail_result['sha256']} · {detail_path}")
                        if detail_path.stat().st_size <= 25 * 1024 * 1024:
                            with detail_path.open('rb') as handle:
                                st.download_button('Download All Records XLSX', handle, file_name=detail_path.name,
                                                   key='all_records_download', on_click='ignore')
                        else:
                            st.info('This export exceeds 25 MB. Open the verified file at the local path above.')
                table(run, 'cdr_details', 'cdr_details', ('HPMN', 'VPMN'))
            elif page == 'Duplicate / Ambiguous Keys':
                st.caption('No records in these groups have been paired. All source members are available below.')
                table(run, 'ambiguous_records', 'ambiguous_records', ('HPMN', 'VPMN'))
        else:
            mapping = {'HPMN Comparison': ('hpmn', ('HPMN',), 'Level 5'), 'VPMN Comparison': ('vpmn', ('VPMN',), 'Level 6'),
                       'HPMN/VPMN Drill-down': ('pairs', ('HPMN', 'VPMN'), 'Level 7')}
            name, operators, level = mapping[page]
            st.header(f'{level} · {page}')
            st.caption('Counts and RATED_AMOUNT_TAP by operator and currency. Signed amount variance = SaaS − Legacy. One-source groups remain visible.')
            table(run, name, name, operators)
