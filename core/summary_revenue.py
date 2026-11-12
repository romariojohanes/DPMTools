"""Independent Summary Revenue reconciliation and complete Excel export."""
from __future__ import annotations

import csv
import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, getcontext
from pathlib import Path
from time import perf_counter

from openpyxl import Workbook, load_workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

from core.engine import Run
from core.monitoring import ResourceMonitor
from core.schema import canonical, resolve_inputs

SUMMARY_FIELDS = ('NO', 'PLMN', 'OPERATOR_NAME', 'FIRST_SEQ', 'LAST_SEQ', 'RECORDS',
                  'CHARGE_SDR', 'TAX_SDR', 'TOTAL_CHARGE_SDR', 'SUBSCRIBERS')
REQUIRED_FIELDS = ('PLMN', 'TOTAL_CHARGE_SDR')
OPTIONAL_FIELDS = tuple(field for field in SUMMARY_FIELDS if field not in REQUIRED_FIELDS)
TEXT_FIELDS = ('NO', 'PLMN', 'OPERATOR_NAME', 'FIRST_SEQ', 'LAST_SEQ')
INTEGER_FIELDS = ('RECORDS', 'SUBSCRIBERS')
FINANCIAL_FIELDS = ('CHARGE_SDR', 'TAX_SDR', 'TOTAL_CHARGE_SDR')
OPTIONAL_COMPARE_FIELDS = ('RECORDS', 'CHARGE_SDR', 'TAX_SDR', 'SUBSCRIBERS')
COMPARE_FIELDS = OPTIONAL_COMPARE_FIELDS[:1] + FINANCIAL_FIELDS + OPTIONAL_COMPARE_FIELDS[-1:]
NULLS = {'', 'NULL', 'NONE', 'N/A', 'NA', '\\N'}


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        while block := handle.read(4 * 1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


@dataclass(frozen=True)
class SummaryInputOptions:
    delimiter: str = ','
    encoding: str = 'utf-8-sig'
    decimal_separator: str = '.'
    sheet_name: str | None = None
    column_mapping: dict[str, str] | None = None

    def __post_init__(self):
        if len(self.delimiter) != 1 or self.delimiter in '\r\n"':
            raise ValueError('CSV delimiter must be one character, excluding quotes/newlines.')
        if self.decimal_separator not in ('.', ','):
            raise ValueError('Decimal separator must be a dot or comma.')
        ''.encode(self.encoding)
        normalized = {}
        for source, target in (self.column_mapping or {}).items():
            source, target = canonical(source), canonical(target)
            if not source or target not in SUMMARY_FIELDS:
                raise ValueError('Summary mappings require a source column and known summary field.')
            if source in normalized or target in normalized.values():
                raise ValueError('Summary column mappings must be one-to-one.')
            normalized[source] = target
        object.__setattr__(self, 'column_mapping', normalized or None)


def inspect_summary_headers(paths, options):
    result = []
    for path in paths:
        path = Path(path)
        if path.suffix.lower() == '.csv':
            with path.open('r', encoding=options.encoding, newline='') as handle:
                values = next(csv.reader(handle, delimiter=options.delimiter, strict=True), None)
        else:
            workbook = load_workbook(path, read_only=True, data_only=False)
            try:
                if options.sheet_name:
                    if options.sheet_name not in workbook.sheetnames:
                        raise ValueError(f'Worksheet not found: {options.sheet_name}')
                    sheet = workbook[options.sheet_name]
                else:
                    if len(workbook.sheetnames) != 1:
                        raise ValueError('Workbook has multiple sheets: specify an explicit worksheet name.')
                    sheet = workbook[workbook.sheetnames[0]]
                values = [cell.value for cell in next(sheet.iter_rows(), ())]
            finally:
                workbook.close()
        if not values:
            raise ValueError(f'Input has no header: {path}')
        names = [canonical(value) if value is not None else '' for value in values]
        if '' in names or len(names) != len(set(names)):
            raise ValueError(f'Blank or duplicate column names after normalization: {path}')
        result.append({'path': str(path), 'headers': [str(v) for v in values], 'normalized_headers': names})
    return result


def _mapped_headers(values, mapping):
    names = [canonical(v) if v is not None else '' for v in values]
    names = [(mapping or {}).get(name, name) for name in names]
    present = [name for name in names if name in SUMMARY_FIELDS]
    if len(present) != len(set(present)):
        raise ValueError('Duplicate summary columns after mapping.')
    missing = set(REQUIRED_FIELDS) - set(present)
    if missing:
        raise ValueError('Missing required summary columns: ' + ', '.join(sorted(missing)))
    return names


def validate_summary_header_mapping(inspected, options):
    for item in inspected:
        try:
            _mapped_headers(item['headers'], options.column_mapping)
        except ValueError as exc:
            raise ValueError(f"{item['path']}: {exc}") from exc


def _number(raw, field, options):
    text = '' if raw is None else str(raw).strip()
    if text.upper() in NULLS:
        raise ValueError('value is required')
    if options.decimal_separator == ',':
        if '.' in text:
            raise ValueError('dot is incompatible with the selected comma decimal separator')
        text = text.replace(',', '.')
    elif ',' in text:
        raise ValueError('comma is incompatible with the selected dot decimal separator')
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError('invalid numeric value') from exc
    if not value.is_finite():
        raise ValueError('numeric value must be finite')
    if field in INTEGER_FIELDS and value != value.to_integral_value():
        raise ValueError('integer value required')
    return value


def _read_source(source, paths, options, issues, emit):
    records = []
    files = []
    for path in paths:
        path = Path(path)
        digest = _sha256(path)
        file_record = {'source': source, 'path': str(path), 'filename': path.name, 'bytes': path.stat().st_size,
                       'sha256_before': digest, 'sha256_after': digest, 'sha256_verified': True,
                       'status': 'READ', 'rows': 0, 'sheet': ''}
        emit('summary_ingestion', source=source, source_file=str(path))
        if path.suffix.lower() == '.csv':
            handle = path.open('r', encoding=options.encoding, newline='')
            reader = csv.reader(handle, delimiter=options.delimiter, strict=True)
            header = next(reader, None)
            rows = reader
            sheet_name = ''
        else:
            workbook = load_workbook(path, read_only=True, data_only=False)
            if options.sheet_name:
                if options.sheet_name not in workbook.sheetnames:
                    workbook.close(); raise ValueError(f'Worksheet not found: {options.sheet_name}')
                sheet = workbook[options.sheet_name]
            else:
                if len(workbook.sheetnames) != 1:
                    workbook.close(); raise ValueError('Workbook has multiple sheets: specify an explicit worksheet name.')
                sheet = workbook[workbook.sheetnames[0]]
            iterator = sheet.iter_rows(values_only=True)
            header = next(iterator, None)
            rows, handle, sheet_name = iterator, None, sheet.title
            file_record['sheet'] = sheet_name
        try:
            if not header:
                raise ValueError('Input has no header.')
            mapped = _mapped_headers(header, options.column_mapping)
            indices = {field: mapped.index(field) for field in SUMMARY_FIELDS if field in mapped}
            unit_indices = [i for i, name in enumerate(mapped) if name in ('CURRENCY', 'UNIT')]
            for row_number, row in enumerate(rows, 2):
                if not any(value is not None and str(value).strip() for value in row):
                    continue
                file_record['rows'] += 1
                for index in unit_indices:
                    if index < len(row) and str(row[index]).strip().upper() not in ('', 'SDR'):
                        issues.append((source, str(path), sheet_name, row_number, 'UNIT', str(row[index]),
                                       'INCOMPATIBLE_UNIT', 'Summary Revenue comparison requires SDR on both sources.'))
                item = {'source_file': str(path), 'source_sheet': sheet_name, 'source_row': row_number}
                for field in TEXT_FIELDS:
                    index = indices.get(field)
                    item[field] = None if index is None else ('' if index >= len(row) or row[index] is None else str(row[index]).strip())
                item['PLMN_NORMALIZED'] = item['PLMN'].upper()
                if not item['PLMN_NORMALIZED']:
                    issues.append((source, str(path), sheet_name, row_number, 'PLMN', item['PLMN'],
                                   'MISSING_KEY', 'PLMN is required.'))
                for field in COMPARE_FIELDS:
                    index = indices.get(field)
                    raw = row[index] if index is not None and index < len(row) else None
                    if index is None or (field in OPTIONAL_FIELDS and (raw is None or str(raw).strip().upper() in NULLS)):
                        item[field] = None
                        continue
                    try:
                        item[field] = _number(raw, field, options)
                    except ValueError as exc:
                        item[field] = None
                        issues.append((source, str(path), sheet_name, row_number, field, '' if raw is None else str(raw),
                                       'INVALID_NUMERIC' if field in REQUIRED_FIELDS else 'INVALID_OPTIONAL_NUMERIC', str(exc)))
                if all(item[field] is not None for field in FINANCIAL_FIELDS) and \
                        item['CHARGE_SDR'] + item['TAX_SDR'] != item['TOTAL_CHARGE_SDR']:
                    issues.append((source, str(path), sheet_name, row_number, 'TOTAL_CHARGE_SDR',
                                   _text(item['TOTAL_CHARGE_SDR']), 'TOTAL_COMPONENT_WARNING',
                                   'Informational: TOTAL_CHARGE_SDR does not equal CHARGE_SDR + TAX_SDR.'))
                records.append(item)
        finally:
            if handle: handle.close()
            if path.suffix.lower() == '.xlsx': workbook.close()
        files.append(file_record)
    counts = {}
    for item in records:
        if item['PLMN_NORMALIZED']:
            counts[item['PLMN_NORMALIZED']] = counts.get(item['PLMN_NORMALIZED'], 0) + 1
    for item in records:
        if item['PLMN_NORMALIZED'] and counts[item['PLMN_NORMALIZED']] > 1:
            issues.append((source, item['source_file'], item['source_sheet'], item['source_row'], 'PLMN', item['PLMN'],
                           'DUPLICATE_KEY', f"PLMN occurs {counts[item['PLMN_NORMALIZED']]} times; rows are not aggregated or paired."))
    return records, files, counts


def _text(value):
    return None if value is None else format(value, 'f')


def execute_summary(legacy_paths, saas_paths, *, legacy_options=None, saas_options=None,
                    settings=None, root='data/runs', progress=None, cancel_event=None):
    # Decimal contexts are thread-local; summaries may contain up to 38-digit source values.
    getcontext().prec = 100
    legacy_paths, saas_paths = resolve_inputs(legacy_paths), resolve_inputs(saas_paths)
    if set(legacy_paths) & set(saas_paths):
        raise ValueError('The same file cannot be used for both sources.')
    run = Run.create(root, settings)
    run.progress = progress
    monitor, started = ResourceMonitor(run, cancel_event), perf_counter()
    run.meta.update(status='INGESTING', reconciliation_type='SUMMARY_REVENUE',
                    options={'legacy': vars(legacy_options or SummaryInputOptions()),
                             'saas': vars(saas_options or SummaryInputOptions())})
    run.save()
    try:
        monitor.start(); issues = []
        legacy, lf, lc = _read_source('legacy', legacy_paths, legacy_options or SummaryInputOptions(), issues, run.emit)
        saas, sf, sc = _read_source('saas', saas_paths, saas_options or SummaryInputOptions(), issues, run.emit)
        run.meta['files'] = lf + sf
        if issues:
            run.con.executemany('INSERT INTO issues VALUES (?,?,?,?,?,?,?,?)', issues)
        warning_codes = {'TOTAL_COMPONENT_WARNING', 'INVALID_OPTIONAL_NUMERIC'}
        blocking_issues = [issue for issue in issues if issue[6] not in warning_codes]
        for record in run.meta['files']:
            record['issue_count'] = sum(1 for issue in issues if issue[0] == record['source'] and issue[1] == record['path'])
            record_blocking = [issue for issue in blocking_issues if issue[0] == record['source'] and issue[1] == record['path']]
            record['invalid_rows'] = len({issue[3] for issue in record_blocking})
            record['status'] = 'INVALID' if record_blocking else ('VALID_WITH_WARNINGS' if record['issue_count'] else 'VALID')
        run.meta['issue_count'] = len(issues)
        run.meta['warning_count'] = len(issues) - len(blocking_issues)
        run.meta['blocking_issue_count'] = len(blocking_issues)
        if blocking_issues:
            run.meta['status'] = 'VALIDATION_FAILED'
        else:
            run.emit('summary_comparison')
            left = {r['PLMN_NORMALIZED']: r for r in legacy}; right = {r['PLMN_NORMALIZED']: r for r in saas}
            columns = ['PLMN','status']
            for prefix in ('legacy','saas'):
                columns += [f'{prefix}_{f.lower()}' for f in ('NO','OPERATOR_NAME','FIRST_SEQ','LAST_SEQ')]
            for field in COMPARE_FIELDS:
                columns += [f'legacy_{field.lower()}', f'saas_{field.lower()}', f'{field.lower()}_difference',
                            f'{field.lower()}_status']
            columns += ['legacy_file','legacy_sheet','legacy_row','saas_file','saas_sheet','saas_row']
            run.con.execute('CREATE TABLE summary_comparison (' + ','.join(f'"{c}" VARCHAR' for c in columns) + ')')
            output = []
            for key in sorted(set(left) | set(right)):
                l, s = left.get(key), right.get(key)
                compared = {field: (l is not None and s is not None and l[field] is not None and s[field] is not None)
                            for field in COMPARE_FIELDS}
                status = ('LEGACY_ONLY' if s is None else 'SAAS_ONLY' if l is None else
                          ('MATCH' if all(not compared[field] or l[field] == s[field] for field in COMPARE_FIELDS)
                           else 'MISMATCH'))
                row = [key, status]
                for item in (l, s):
                    row += [('N/A' if item is None or item.get(field) is None else item[field])
                            for field in ('NO','OPERATOR_NAME','FIRST_SEQ','LAST_SEQ')]
                for field in COMPARE_FIELDS:
                    field_status = ('NOT_COMPARED' if not compared[field] else
                                    ('MATCH' if l[field] == s[field] else 'MISMATCH'))
                    row += [_text(l[field]) if l and l[field] is not None else 'N/A',
                            _text(s[field]) if s and s[field] is not None else 'N/A',
                            _text(s[field] - l[field]) if compared[field] else 'N/A', field_status]
                for item in (l, s): row += [item['source_file'],item['source_sheet'],str(item['source_row'])] if item else [None,None,None]
                output.append(row)
            if output: run.con.executemany('INSERT INTO summary_comparison VALUES (' + ','.join('?' for _ in columns) + ')', output)
            run.con.execute("CREATE VIEW summary_legacy_only AS SELECT * FROM summary_comparison WHERE status='LEGACY_ONLY'")
            run.con.execute("CREATE VIEW summary_saas_only AS SELECT * FROM summary_comparison WHERE status='SAAS_ONLY'")
            summary = []
            for field in COMPARE_FIELDS:
                lv = [r[field] for r in legacy if r[field] is not None]
                sv = [r[field] for r in saas if r[field] is not None]
                lt = sum(lv, Decimal(0)); st = sum(sv, Decimal(0))
                complete = len(lv) == len(legacy) and len(sv) == len(saas)
                field_status = 'NOT_COMPARED' if not complete else ('MATCH' if lt == st else 'MISMATCH')
                summary.append((field, _text(lt) if lv else 'N/A', _text(st) if sv else 'N/A',
                                _text(st-lt) if complete else 'N/A', field_status))
            run.con.execute('CREATE TABLE summary_totals (metric VARCHAR, legacy_value VARCHAR, saas_value VARCHAR, difference VARCHAR, status VARCHAR)')
            run.con.executemany('INSERT INTO summary_totals VALUES (?,?,?,?,?)', summary)
            statuses = {status: sum(1 for row in output if row[1] == status) for status in ('MATCH','MISMATCH','LEGACY_ONLY','SAAS_ONLY')}
            run.meta.update(status='COMPLETED', result='MATCH' if all(row[1] == 'MATCH' for row in output) else 'MISMATCH',
                            operator_counts={'legacy': len(legacy), 'saas': len(saas), **statuses})
        run.save()
    except Exception as exc:
        run.meta.update(status='CANCELLED' if monitor.stop_reason == 'USER_CANCELLED' else 'ABORTED_RESOURCE_LIMIT' if monitor.stop_reason else 'FAILED', error=str(exc))
        run.save()
    finally:
        monitor.finish(); run.emit('finished'); run.meta['completed_at'] = datetime.now(timezone.utc).isoformat()
        run.meta['processing_seconds'] = round(perf_counter()-started, 3); run.save(); run.close()
    return run.directory


def export_summary_xlsx(run, destination=None, checkpoint=None):
    if run.meta.get('reconciliation_type') != 'SUMMARY_REVENUE' or run.meta.get('status') != 'COMPLETED':
        raise ValueError('Summary export requires a completed Summary Revenue run.')
    folder = Path(destination or run.directory / 'reports'); folder.mkdir(parents=True, exist_ok=True)
    target, temporary = folder / 'Summary_Revenue_All_Records.xlsx', folder / 'Summary_Revenue_All_Records.tmp.xlsx'
    workbook = Workbook(write_only=True)
    def sheet(name, headers):
        ws = workbook.create_sheet(name); ws.freeze_panes = 'A2'; ws.auto_filter.ref = f'A1:{get_column_letter(len(headers))}1'
        cells=[]
        for value in headers:
            cell=WriteOnlyCell(ws,value=value); cell.font=Font(bold=True,color='FFFFFF'); cell.fill=PatternFill('solid',fgColor='183153'); cells.append(cell)
        ws.append(cells)
        for i, header in enumerate(headers,1): ws.column_dimensions[get_column_letter(i)].width = 42 if header.endswith('_file') else 22
        return ws
    summary = sheet('Executive Summary', ['property','legacy_value','saas_value','difference','status'])
    counts=run.meta['operator_counts']; summary.append(['overall_status',run.meta['result'],'','',''])
    summary.append(['operator_count',str(counts['legacy']),str(counts['saas']),str(counts['saas']-counts['legacy']),''])
    for row in run.con.execute('SELECT * FROM summary_totals').fetchall(): summary.append(list(row))
    cursor=run.con.execute('SELECT * FROM summary_comparison ORDER BY PLMN'); headers=[c[0] for c in cursor.description]
    detail=sheet('PLMN Comparison',headers); written=0
    while rows:=cursor.fetchmany(2000):
        for row in rows: detail.append(list(row)); written+=1
    for title,status in (('Legacy Only','LEGACY_ONLY'),('SaaS Only','SAAS_ONLY')):
        cursor=run.con.execute('SELECT * FROM summary_comparison WHERE status=? ORDER BY PLMN',[status]); headers=[c[0] for c in cursor.description]
        ws=sheet(title,headers)
        for row in cursor.fetchall(): ws.append(list(row))
    issues = sheet('Validation Warnings', ['source','source_file','source_sheet','source_row','field','raw_value','code','message'])
    for row in run.con.execute('SELECT * FROM issues ORDER BY source,source_file,source_row,field').fetchall():
        issues.append(list(row))
    integrity=sheet('Integrity',['property','value']); integrity.append(['expected_comparison_rows',str(written)])
    try:
        workbook.save(temporary)
        check=load_workbook(temporary,read_only=True,data_only=False)
        try:
            verified=max(sum(1 for _ in check['PLMN Comparison'].iter_rows())-1,0)
        finally:
            check.close()
        if verified != written: raise ValueError(f'XLSX row count mismatch: expected {written}, found {verified}.')
        temporary.replace(target)
    except BaseException:
        temporary.unlink(missing_ok=True); raise
    digest=_sha256(target)
    return target,written,digest
