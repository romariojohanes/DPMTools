"""Streaming Excel output; financial decimals are exact text, never Excel floats."""
import hashlib
from decimal import Decimal
from pathlib import Path
from time import perf_counter

from core.schema import quote

from openpyxl import Workbook, load_workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

REPORTS = {
    'Executive Summary': ['counts', 'revenue', 'hpmn_set', 'vpmn_set', 'matching_summary', 'revenue_attribution'],
    'HPMN Comparison': ['hpmn', 'hpmn_set'],
    'VPMN Comparison': ['vpmn', 'vpmn_set'],
    'HPMN VPMN Comparison': ['pairs'],
    'Data Ingestion Validation': ['validation_files', 'issues'],
    'Operator Comparison': ['hpmn', 'vpmn', 'pairs'],
    'CDR Matching Summary': ['matching_summary'],
    'Rating Mismatch': ['rating_differences'],
    'Legacy Only': ['legacy_only'],
    'SaaS Only': ['saas_only'],
    'Duplicate Ambiguous': ['duplicate_keys', 'ambiguous_records'],
    'Field-Level Differences': ['field_differences'],
    'Revenue Variance Attribution': ['revenue_attribution'],
}
EXCEL_MAX_ROWS = 1_048_576
EXTRACT_LIMIT = 50_000
DETAIL_TABLES = {'cdr_details', 'rating_differences', 'legacy_only', 'saas_only', 'duplicate_keys',
                 'ambiguous_records', 'field_differences', 'revenue_attribution', 'issues'}


def _checkpoint(callback, event, **details):
    if callback is not None:
        callback({'event': event, **details})


def _cells(sheet, values, header=False):
    for value in values:
        if isinstance(value, Decimal):
            value = format(value, 'f')
        # Excel counts can also exceed 15 significant digits; preserve such integers.
        if isinstance(value, int) and not isinstance(value, bool) and abs(value) >= 10**15:
            value = str(value)
        cell = WriteOnlyCell(sheet, value=value)
        if isinstance(value, str):
            cell.data_type = 's'  # prevent formula injection from filenames/raw input
        if header:
            cell.font = Font(bold=True, color='FFFFFF')
            cell.fill = PatternFill('solid', fgColor='183153')
        yield cell


def _sheet(workbook, name, headers):
    sheet = workbook.create_sheet(name[:31])
    sheet.freeze_panes = 'A2'
    sheet.append(list(_cells(sheet, headers, True)))
    return sheet


def _format_detail_sheet(sheet, headers):
    """Apply review-friendly formatting without retaining record data in memory."""
    sheet.auto_filter.ref = f'A1:{get_column_letter(len(headers))}1'
    for index, header in enumerate(headers, 1):
        width = 52 if header in ('match_key', 'legacy_file', 'saas_file') else 24
        if header in ('status', 'profile'):
            width = 28
        elif header.endswith('_row') or header.endswith('_count'):
            width = 16
        sheet.column_dimensions[get_column_letter(index)].width = width


def _sha256_file(path, block_size=4 * 1024 * 1024):
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        while block := handle.read(block_size):
            digest.update(block)
    return digest.hexdigest()


def export_all_records_xlsx(run, destination=None, checkpoint=None):
    """Stream every CDR detail row to XLSX, independently of UI pagination."""
    if run.meta['status'] != 'COMPLETED':
        raise ValueError('Record export requires a completed run.')
    destination = Path(destination or run.directory / 'reports')
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / 'CDR_Matching_Details_All_Records.xlsx'
    temporary = target.with_suffix('.tmp.xlsx')
    workbook = Workbook(write_only=True)
    cursor = run.con.execute('''SELECT d.*,
        coalesce(l.IMSI,s.IMSI) AS IMSI, coalesce(l.ANUM,s.ANUM) AS ANUM,
        coalesce(l.BNUM,s.BNUM) AS BNUM,
        coalesce(l.GPRS_CHARGING_ID,s.GPRS_CHARGING_ID) AS GPRS_CHARGING_ID,
        l.TAX_AMOUNT_TAP AS legacy_tax_amount, s.TAX_AMOUNT_TAP AS saas_tax_amount,
        l.FINAL_TAX_AMOUNT AS legacy_final_tax_amount, s.FINAL_TAX_AMOUNT AS saas_final_tax_amount,
        l.RATE AS legacy_rate, s.RATE AS saas_rate,
        d.rating_difference AS revenue_variance
        FROM cdr_details d LEFT JOIN legacy l ON d.legacy_id=l.record_id
        LEFT JOIN saas s ON d.saas_id=s.record_id ORDER BY d.match_key''')
    headers = [column[0] for column in cursor.description]
    sheet = _sheet(workbook, 'CDR Matching Details', headers)
    _format_detail_sheet(sheet, headers)
    written = 0
    _checkpoint(checkpoint, 'all_records_export_start', target=str(target))
    try:
        while rows := cursor.fetchmany(2000):
            for row in rows:
                if written and written % (EXCEL_MAX_ROWS - 1) == 0:
                    sheet = _sheet(workbook, f'CDR Details {written // (EXCEL_MAX_ROWS - 1) + 1}', headers)
                    _format_detail_sheet(sheet, headers)
                sheet.append(list(_cells(sheet, row)))
                written += 1
            _checkpoint(checkpoint, 'all_records_batch', rows_written=written)
        workbook.save(temporary)
        verification = load_workbook(temporary, read_only=True, data_only=False)
        try:
            verified_rows = sum(max(sum(1 for _ in sheet.iter_rows()) - 1, 0)
                                for sheet in verification.worksheets)
            if verified_rows != written:
                raise ValueError(f'XLSX row count mismatch: expected {written}, found {verified_rows}.')
        finally:
            verification.close()
        temporary.replace(target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    digest = _sha256_file(target)
    _checkpoint(checkpoint, 'all_records_export_complete', target=str(target), rows=written,
                bytes=target.stat().st_size, sha256=digest)
    return target, written, digest


def _query_sheet(workbook, run, table, limit=EXTRACT_LIMIT, checkpoint=None):
    order = ' ORDER BY source,match_key,record_id' if table == 'ambiguous_records' else ''
    _checkpoint(checkpoint, 'table_extract_start', table=table, limit=limit)
    cursor = run.con.execute(f'SELECT * FROM {table}{order} LIMIT {int(limit)}')  # internal table names only
    headers = [c[0] for c in cursor.description]
    part, written = 1, 1
    sheet = _sheet(workbook, table, headers)
    while rows := cursor.fetchmany(2000):
        for row in rows:
            if written == EXCEL_MAX_ROWS:
                part += 1
                sheet = _sheet(workbook, f'{table}_{part}', headers)
                written = 1
            sheet.append(list(_cells(sheet, row)))
            written += 1
        _checkpoint(checkpoint, 'table_batch', table=table, rows_written=written - 1)
    _checkpoint(checkpoint, 'table_extract_complete', table=table, rows_written=written - 1)


def export_report(run, report, destination=None, checkpoint=None):
    started = perf_counter()
    if report not in REPORTS:
        raise ValueError('Unknown report.')
    if report != 'Data Ingestion Validation' and run.meta['status'] != 'COMPLETED':
        raise ValueError('Reconciliation reports require a successfully validated, completed run.')
    destination = Path(destination or run.directory / 'reports')
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / (report.replace(' ', '_') + '.xlsx')
    _checkpoint(checkpoint, 'report_start', report=report, target=str(target))
    workbook = Workbook(write_only=True)
    sheet = _sheet(workbook, 'Run Metadata', ['property', 'value'])
    values = {key: run.meta.get(key) for key in ('run_id', 'status', 'result', 'traffic_date', 'created_at', 'completed_at', 'processing_seconds', 'issue_count', 'error')}
    values.update(date_ranges=str(run.meta.get('date_ranges', {})), normalization_options=str(run.meta.get('options', {})),
                  financial_policy='Exact DECIMAL(38,scale); amounts and variances exported as TEXT to retain all digits.',
                  variance_definition='Signed variance = SaaS - Legacy; absolute variance = abs(SaaS - Legacy).',
                  scope='Aggregate and composite-key reconciliation. Duplicate keys are unresolved; no arbitrary pairing.',
                  extract_policy=f'Each Excel result table is bounded to {EXTRACT_LIMIT} rows. Use CSV/Parquet for complete detail.',
                  matching_profiles=str(run.meta.get('matching_profiles', {})),
                  stage_seconds=str(run.meta.get('stage_seconds', {})))
    for key, value in values.items():
        sheet.append(list(_cells(sheet, [key, 'See actual source date ranges' if value is None and key == 'traffic_date' else 'Not applicable' if value is None else str(value)])))
    columns = ['source', 'filename', 'path', 'sheet', 'bytes', 'modified_ns', 'sha256_before', 'sha256_after',
               'sha256_verified', 'sha256_seconds', 'rows', 'status', 'issue_count', 'invalid_rows', 'error']
    sheet = _sheet(workbook, 'Source Files', columns)
    for record in run.meta['files']:
        sheet.append(list(_cells(sheet, [record.get(key) for key in columns])))
    coverage = _sheet(workbook, 'Extract Coverage', ['table', 'available_rows', 'exported_rows', 'truncated'])
    for table in REPORTS[report]:
        if run.con.execute('SELECT count(*) FROM information_schema.tables WHERE table_name=?', [table]).fetchone()[0]:
            _checkpoint(checkpoint, 'table_count_start', report=report, table=table)
            has_members = run.con.execute(
                "SELECT count(*) FROM information_schema.tables WHERE table_name='ambiguous_members'"
            ).fetchone()[0]
            count_table = 'ambiguous_members' if table == 'ambiguous_records' and has_members else table
            total = run.con.execute(f'SELECT count(*) FROM {count_table}').fetchone()[0]
            _checkpoint(checkpoint, 'table_count_complete', report=report, table=table, rows=total)
            coverage.append(list(_cells(coverage, [table, total, min(total, EXTRACT_LIMIT), total > EXTRACT_LIMIT])))
            _query_sheet(workbook, run, table, checkpoint=checkpoint)
    temporary = target.with_suffix('.tmp.xlsx')
    _checkpoint(checkpoint, 'workbook_save_start', report=report, temporary=str(temporary))
    try:
        workbook.save(temporary)
        verification = load_workbook(temporary, read_only=True, data_only=False)
        try:
            required = {'Run Metadata', 'Source Files', 'Extract Coverage'}
            if not required.issubset(verification.sheetnames):
                raise ValueError('Generated workbook is missing required evidence sheets.')
        finally:
            verification.close()
        _checkpoint(checkpoint, 'workbook_save_complete', report=report, bytes=temporary.stat().st_size)
        temporary.replace(target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    run.meta['reporting_seconds'] = run.meta.get('reporting_seconds', 0) + perf_counter() - started
    run.save()
    _checkpoint(checkpoint, 'report_complete', report=report, target=str(target), bytes=target.stat().st_size)
    return target


def export_detail(run, table, format='csv', destination=None, filters=None, checkpoint=None):
    """DuckDB streams complete local detail without Python dataframe materialization."""
    if table not in DETAIL_TABLES or format not in ('csv', 'parquet'):
        raise ValueError('Unknown detail table or export format.')
    if run.meta['status'] != 'COMPLETED' and table != 'issues':
        raise ValueError('Detail exports require a completed run.')
    columns = [row[0] for row in run.con.execute(f'DESCRIBE {table}').fetchall()]
    conditions, parameters = [], []
    for field, value in (filters or {}).items():
        if field not in columns:
            raise ValueError('Unknown filter column.')
        if value is None:
            conditions.append(f'"{field}" IS NULL')
        else:
            conditions.append(f'"{field}" = ?')
            parameters.append(value)
    where = ' WHERE ' + ' AND '.join(conditions) if conditions else ''
    folder = Path(destination or run.directory / 'reports')
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f'{table}.{format}'
    temporary = target.with_suffix('.tmp.' + format)
    started = perf_counter()
    _checkpoint(checkpoint, 'parquet_export_start' if format == 'parquet' else 'csv_export_start',
                table=table, target=str(target))
    options = 'FORMAT CSV, HEADER TRUE' if format == 'csv' else 'FORMAT PARQUET, COMPRESSION ZSTD'
    previous_threads = run.con.execute("SELECT current_setting('threads')").fetchone()[0]
    run.con.execute('SET threads=1')
    try:
        run.con.execute(f'COPY (SELECT * FROM {table}{where}) TO {quote(str(temporary))} ({options})', parameters)
        if format == 'parquet':
            exported_columns = [row[0] for row in run.con.execute(
                'DESCRIBE SELECT * FROM read_parquet(?)', [str(temporary)]).fetchall()]
            if exported_columns != columns:
                raise ValueError(f'{table} Parquet export schema does not match the source table.')
            exported_rows = run.con.execute(
                'SELECT count(*) FROM read_parquet(?)', [str(temporary)]).fetchone()[0]
            expected_rows = run.con.execute(
                f'SELECT count(*) FROM {table}{where}', parameters).fetchone()[0]
            if exported_rows != expected_rows:
                raise ValueError(f'{table} Parquet export row count mismatch: expected {expected_rows}, found {exported_rows}.')
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    finally:
        run.con.execute(f'SET threads={int(previous_threads)}')
    temporary.replace(target)
    run.meta['reporting_seconds'] = run.meta.get('reporting_seconds', 0) + perf_counter() - started
    run.save()
    _checkpoint(checkpoint, 'parquet_export_complete' if format == 'parquet' else 'csv_export_complete',
                table=table, target=str(target), bytes=target.stat().st_size)
    return target
