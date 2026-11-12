"""Strict ingestion: DuckDB CSV scans, bounded XLSX batches, no Pandas."""
import csv
import hashlib
import shutil
from xml.etree.ElementTree import iterparse
from zipfile import ZipFile
from decimal import Decimal
from pathlib import Path
from time import perf_counter

from openpyxl import load_workbook

from core.schema import FIELDS, IDENTIFIERS, NULLS, NUMERIC, REQUIRED_VALUES, InputOptions, canonical, quote
from core.xlsx_stream import streaming_workbook


def _sha256(path, block_size=4 * 1024 * 1024):
    started = perf_counter()
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        while block := handle.read(block_size):
            digest.update(block)
    return digest.hexdigest(), perf_counter() - started


def _xml_numbers(archive, sheet_path):
    """Bounded XML cursor: retain numeric lexemes before openpyxl float conversion."""
    ns = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
    with archive.open(sheet_path.lstrip('/')) as handle:
        parser = iterparse(handle, events=('start', 'end'))
        _, root = next(parser)
        sheet_data = None
        for event, element in parser:
            if event == 'start' and element.tag == ns + 'sheetData':
                sheet_data = element
            if event == 'end' and element.tag == ns + 'row':
                values = {}
                for cell in element:
                    value = cell.find(ns + 'v')
                    if cell.get('t', 'n') == 'n' and value is not None:
                        values[cell.get('r')] = value.text
                yield int(element.get('r')), values
                element.clear()
                if sheet_data is not None:
                    sheet_data.clear()


def _safe_identifier(value):
    """Excel guarantees only 15 decimal digits. Never infer lost digits/zeros."""
    try:
        number = Decimal(value)
        return number.is_finite() and number >= 0 and number == number.to_integral_value() and number < 10**15
    except Exception:
        return False


def _headers(values):
    return _mapped_headers(values)


def _mapped_headers(values, mapping=None):
    aliases = {field.replace('_', ''): field for field in FIELDS}
    names = [canonical(v) if v is not None else '' for v in values]
    mapping = mapping or {}
    names = [mapping.get(name, aliases.get(name.replace('_', ''), name)) for name in names]
    if len(set(names)) != len(names) or '' in names:
        raise ValueError('Blank or duplicate column names after normalization.')
    missing = set(FIELDS) - set(names)
    if missing:
        raise ValueError('Missing required columns: ' + ', '.join(sorted(missing)))
    return names


def inspect_headers(paths, options: InputOptions):
    """Read only input headers for UI mapping; never ingest or alter source files."""
    inspected = []
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
                sheet.reset_dimensions()
                values = [cell.value for cell in next(sheet.iter_rows(), ())]
            finally:
                workbook.close()
        if not values:
            raise ValueError(f'Input has no header: {path}')
        normalized = [canonical(value) if value is not None else '' for value in values]
        if '' in normalized or len(set(normalized)) != len(normalized):
            raise ValueError(f'Blank or duplicate column names after normalization: {path}')
        inspected.append({'path': str(path), 'headers': [str(value) for value in values],
                          'normalized_headers': normalized})
    return inspected


def validate_header_mapping(inspected, options: InputOptions):
    """Validate one source mapping against every inspected file header."""
    results = []
    for item in inspected:
        try:
            mapped = _mapped_headers(item['headers'], options.column_mapping)
        except ValueError as exc:
            raise ValueError(f"{item['path']}: {exc}") from exc
        results.append({'path': item['path'], 'mapped_headers': mapped})
    return results


def _issue(run, source, path, sheet, row, field, raw, code, message):
    run.con.execute('INSERT INTO issues VALUES (?,?,?,?,?,?,?,?)',
                    [source, str(path), sheet, row, field, raw, code, message])


def _csv(run, source, path, options):
    run.emit('csv_provenance', source=source, source_file=str(path))
    row_map = run.directory / 'csv_rows.csv'
    with path.open('r', encoding=options.encoding, newline='') as handle:
        reader = csv.reader(handle, delimiter=options.delimiter, strict=True)
        original = next(reader, None)
        if not original:
            raise ValueError('CSV has no header.')
        headers = _mapped_headers(original, options.column_mapping)
        # DuckDB does not expose CSV physical line offsets. Stream only a small
        # provenance index, keeping full parsing/type conversion in DuckDB.
        with row_map.open('w', encoding='ascii', newline='') as index:
            index.write('record_number,source_row\n')
            ordinal = 0
            while True:
                physical_line = reader.line_num + 1
                record = next(reader, None)
                if record is None:
                    break
                if not record:
                    continue  # completely blank physical lines are not CDR records
                if len(record) != len(headers):
                    raise ValueError(f'CSV physical line {physical_line}: expected {len(headers)} fields, found {len(record)}.')
                ordinal += 1
                index.write(f'{ordinal},{physical_line}\n')
                if ordinal % 100000 == 0:
                    run.emit('csv_provenance', records=ordinal, source_file=str(path), source=source)
    converted = None
    try:
        # DuckDB core handles UTF-8; other configured codecs are transcoded in bounded chunks.
        scan = path
        if options.encoding.lower().replace('_', '-') not in ('utf-8', 'utf-8-sig', 'utf8'):
            converted = run.directory / 'transcoded.csv'
            with path.open('r', encoding=options.encoding, newline='') as src, converted.open('w', encoding='utf-8', newline='') as dst:
                shutil.copyfileobj(src, dst, length=1024 * 1024)
            scan = converted
        cols = '{' + ','.join(f'{quote(name)}: \'VARCHAR\'' for name in headers) + '}'
        projection = ','.join('"' + f + '"' for f in FIELDS)
        # Explicit columns prevent type inference or header-driven identifier corruption.
        run.emit('csv_scan', source=source, source_file=str(path))
        sql = f'''CREATE OR REPLACE TEMP TABLE stage AS
            SELECT {projection}, {quote(str(path))} AS source_file, '' AS source_sheet,
                   row_number() OVER () + 1 AS source_row
            FROM read_csv({quote(str(scan))}, columns={cols}, header=true, auto_detect=false,
                delim={quote(options.delimiter)}, quote='"', escape='"', parallel=false,
                strict_mode=true, ignore_errors=false, null_padding=false, nullstr='')'''
        run.con.execute(sql)
        count = run.con.execute('SELECT count(*) FROM stage').fetchone()[0]
        if count != ordinal:
            raise ValueError('CSV reader/provenance row counts disagree; no rows accepted.')
        # Stage source_row is the record ordinal + 1 until mapped to physical lines.
        target_columns = ','.join('"' + f + '"' for f in FIELDS + ['source_file', 'source_sheet', 'source_row'])
        run.con.execute(f'''INSERT INTO raw_{source} ({target_columns})
            SELECT {','.join('s."' + f + '"' for f in FIELDS)}, s.source_file, s.source_sheet, p.source_row
            FROM stage s JOIN read_csv({quote(str(row_map))}, header=true,
                columns={{'record_number':'BIGINT','source_row':'BIGINT'}}) p
            ON s.source_row=p.record_number+1''')
        run.emit('csv_scan', source=source, source_file=str(path), records=count)
        return count, ''
    finally:
        run.con.execute('DROP TABLE IF EXISTS stage')
        if converted:
            converted.unlink(missing_ok=True)
        row_map.unlink(missing_ok=True)


def _xlsx(run, source, path, options):
    workbook_context = streaming_workbook(path, run.directory)
    workbook = workbook_context.__enter__()
    try:
        if options.sheet_name:
            if options.sheet_name not in workbook.sheetnames:
                raise ValueError(f'Worksheet not found: {options.sheet_name}')
            sheet = workbook[options.sheet_name]
        else:
            if len(workbook.sheetnames) != 1:
                raise ValueError('Workbook has multiple sheets: specify an explicit worksheet name.')
            sheet = workbook[workbook.sheetnames[0]]
        # Some exporters write stale worksheet dimensions; read all actual XML rows.
        sheet.reset_dimensions()
        rows = sheet.iter_rows()
        headers = _mapped_headers([cell.value for cell in next(rows, ())], options.column_mapping)
        indices = [headers.index(field) for field in FIELDS]
        count = 0
        spool = run.directory / 'xlsx_stage.csv'
        issue_spool = run.directory / 'xlsx_issues.csv'
        with ZipFile(path) as archive, spool.open('w', encoding='utf-8', newline='') as output, issue_spool.open('w', encoding='utf-8', newline='') as issue_output:
            numeric_rows = iter(_xml_numbers(archive, sheet._worksheet_path))
            current = next(numeric_rows, (None, {}))
            writer, issue_writer = csv.writer(output), csv.writer(issue_output)
            writer.writerow(FIELDS + ['source_file', 'source_sheet', 'source_row', 'native_numeric'])
            issue_writer.writerow(['source', 'source_file', 'source_sheet', 'source_row', 'field', 'raw_value', 'code', 'message'])
            for number, cells in enumerate(rows, start=2):
                if all(cell.value is None for cell in cells):
                    continue
                while current[0] is not None and current[0] < number:
                    current = next(numeric_rows, (None, {}))
                lexemes = current[1] if current[0] == number else {}
                values, native_fields = [], []
                for field, index in zip(FIELDS, indices):
                    cell = cells[index] if index < len(cells) else None
                    value = cell.value if cell else None
                    raw_number = lexemes.get(cell.coordinate) if cell and value is not None else None
                    if cell and cell.data_type == 'f':
                        issue_writer.writerow((source, str(path), sheet.title, number, field, str(value),
                                               'UNSAFE_EXCEL_CELL', 'Excel formulas are unsupported; supply literal values.'))
                    if field in IDENTIFIERS and value is not None and not isinstance(value, str):
                        if raw_number is None or not _safe_identifier(raw_number):
                            issue_writer.writerow((source, str(path), sheet.title, number, field, str(value),
                                                   'POTENTIALLY_CORRUPTED_IDENTIFIER', 'Numeric Excel identifier must be a nonnegative integer of at most 15 digits; verify against source text.'))
                    if field == 'EVENT_DATE' and hasattr(value, 'strftime'):
                        if hasattr(value, 'hour') and (value.hour or value.minute or value.second or value.microsecond):
                            issue_writer.writerow((source, str(path), sheet.title, number, field, str(value), 'DATE_HAS_TIME', 'EVENT_DATE must contain a date without a time.'))
                        value = value.strftime(options.date_format)
                    elif raw_number is not None and field in NUMERIC + IDENTIFIERS:
                        # XML numeric tokens preserve every stored digit, including exponents.
                        value = raw_number
                        native_fields.append(field)
                    values.append(None if value is None else str(value))
                writer.writerow((*values, str(path), sheet.title, number, '|' + '|'.join(native_fields) + '|'))
                count += 1
                if count % 10000 == 0:
                    run.emit('ingestion', records=count, source_file=str(path), source=source)
        run.con.execute('BEGIN TRANSACTION')
        try:
            cols = '{' + ','.join(quote(f) + ": 'VARCHAR'" for f in FIELDS + ['source_file', 'source_sheet']) + ", 'source_row': 'BIGINT', 'native_numeric': 'VARCHAR'}"
            target = ','.join('"' + f + '"' for f in FIELDS + ['source_file', 'source_sheet', 'source_row', 'native_numeric'])
            run.con.execute(f"INSERT INTO raw_{source} ({target}) SELECT * FROM read_csv({quote(str(spool))}, header=true, columns={cols}, auto_detect=false, nullstr='')")
            issue_cols = "{'source':'VARCHAR','source_file':'VARCHAR','source_sheet':'VARCHAR','source_row':'BIGINT','field':'VARCHAR','raw_value':'VARCHAR','code':'VARCHAR','message':'VARCHAR'}"
            run.con.execute(f"INSERT INTO issues SELECT * FROM read_csv({quote(str(issue_spool))}, header=true, columns={issue_cols}, auto_detect=false)")
            run.con.execute('COMMIT')
        except Exception:
            run.con.execute('ROLLBACK')
            raise
        finally:
            spool.unlink(missing_ok=True)
            issue_spool.unlink(missing_ok=True)
        return count, sheet.title
    finally:
        workbook_context.__exit__(None, None, None)


def ingest(run, source, paths, options: InputOptions):
    if source not in ('legacy', 'saas'):
        raise ValueError('Unknown source.')
    definitions = ', '.join(f'"{field}" VARCHAR' for field in FIELDS)
    run.con.execute(f'CREATE SEQUENCE raw_{source}_sequence START 1')
    run.con.execute(f"CREATE TABLE raw_{source} ({definitions}, source_file VARCHAR, source_sheet VARCHAR, source_row BIGINT, native_numeric VARCHAR DEFAULT '', ingest_id BIGINT DEFAULT nextval('raw_{source}_sequence'))")
    for path in paths:
        path = Path(path)
        before = path.stat()
        record = {'source': source, 'filename': path.name, 'path': str(path), 'bytes': before.st_size,
                  'modified_ns': before.st_mtime_ns, 'rows': 0, 'sheet': '', 'status': 'FAILED'}
        try:
            record['sha256_before'], before_hash_seconds = _sha256(path)
            record['sha256_seconds'] = before_hash_seconds
            run.emit('ingestion', source=source, source_file=str(path))
            record['rows'], record['sheet'] = (_csv if path.suffix.lower() == '.csv' else _xlsx)(run, source, path, options)
            after = path.stat()
            record['sha256_after'], after_hash_seconds = _sha256(path)
            record['sha256_seconds'] += after_hash_seconds
            record['sha256_verified'] = record['sha256_before'] == record['sha256_after']
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                _issue(run, source, path, '', None, '', None, 'FILE_CHANGED', 'File changed during ingestion; use stable snapshots.')
            if not record['sha256_verified']:
                _issue(run, source, path, '', None, '', None, 'FILE_HASH_CHANGED', 'SHA-256 changed during ingestion; use stable snapshots.')
            record['status'] = 'READ'
            if record['rows'] == 0:
                _issue(run, source, path, record['sheet'], None, '', None, 'EMPTY_FILE', 'Input contains no CDR rows.')
        except Exception as exc:
            record['error'] = str(exc)
            _issue(run, source, path, '', None, '', None, 'FILE_ERROR', str(exc))
        finally:
            if 'sha256_after' not in record and path.exists():
                try:
                    record['sha256_after'], seconds = _sha256(path)
                    record['sha256_seconds'] = record.get('sha256_seconds', 0) + seconds
                    record['sha256_verified'] = record.get('sha256_before') == record['sha256_after']
                except OSError:
                    record['sha256_verified'] = False
            # A structural failure can happen before DuckDB's scan/finally block.
            (run.directory / 'csv_rows.csv').unlink(missing_ok=True)
            (run.directory / 'xlsx_stage.csv').unlink(missing_ok=True)
            (run.directory / 'xlsx_issues.csv').unlink(missing_ok=True)
        run.meta['files'].append(record)
        run.save()
    run.emit('normalization', source=source)
    normalize(run, source, options)


def normalize(run, source, options):
    nulls = ','.join(quote(v) for v in NULLS)
    clean = []
    for field in FIELDS:
        value = f'trim("{field}")'
        value = f'CASE WHEN upper({value}) IN ({nulls}) THEN NULL ELSE {value} END'
        if field in ('HPMN', 'VPMN', 'CURRENCY', 'SERVICE_TYPE'):
            value = f'upper({value})'
        clean.append(f'{value} AS "{field}"')
    originals = ','.join(f'{f} := "{f}"' for f in FIELDS)
    numeric_updates = []
    for field in NUMERIC:
        ref = f'"{field}"'
        numeric = f"replace({ref}, ',', '.')" if options.decimal_separator == ',' else ref
        expanded = f"CASE WHEN contains(lower({numeric}), 'e') THEN exact_number({numeric}) ELSE {numeric} END"
        value = f"CASE WHEN contains({ref}, '.') THEN NULL ELSE {expanded} END" if options.decimal_separator == ',' else expanded
        if options.decimal_separator == ',':
            native = f"CASE WHEN contains(lower({ref}), 'e') THEN exact_number({ref}) ELSE {ref} END"
            value = f"CASE WHEN contains(native_numeric, '|{field}|') THEN {native} ELSE {value} END"
        numeric_updates.append(f'{ref} = {value}')
    identifier_updates = []
    for field in IDENTIFIERS:
        identifier_updates.append(f'''expanded_{field} = CASE WHEN contains(lower("{field}"), 'e') THEN exact_number("{field}") ELSE "{field}" END''')
    observed_scale = ','.join(f'''coalesce(max(CASE WHEN regexp_full_match("{f}", '[+-]?[0-9]+([.][0-9]+)?')
        THEN length(split_part("{f}",'.',2)) END),0)''' for f in NUMERIC)
    errors, typed = [], []
    scale = run.settings.decimal_scale
    numeric_type = f'DECIMAL(38,{scale})'
    date_value = f'CAST(try_strptime("EVENT_DATE", {quote(options.date_format)}) AS DATE)'
    for field in FIELDS:
        ref = f'"{field}"'
        if field in REQUIRED_VALUES:
            errors.append((field, f'{ref} IS NULL', 'REQUIRED_VALUE', 'Required reconciliation value is missing.'))
        if field in NUMERIC:
            pattern = r'[+-]?[0-9]+([.][0-9]+)?'
            decimal = ref
            valid = f"regexp_full_match({ref}, {quote(pattern)}) AND length(split_part({decimal}, '.', 2)) <= {scale} AND try_cast({decimal} AS {numeric_type}) IS NOT NULL"
            original = f'original."{field}"'
            present = f"{original} IS NOT NULL AND upper(trim({original})) NOT IN ({nulls})"
            errors.append((field, f'{present} AND ({ref} IS NULL OR NOT ({valid}))', 'INVALID_DECIMAL', f'Expected exact {numeric_type}; no grouping separators; maximum {scale} fractional digits.'))
            typed.append(f'CASE WHEN {valid} THEN try_cast({decimal} AS {numeric_type}) END AS {ref}')
        elif field == 'EVENT_DATE':
            errors.append((field, f'{ref} IS NOT NULL AND ({date_value} IS NULL OR strftime({date_value}, {quote(options.date_format)}) <> {ref})', 'INVALID_DATE', 'Date does not match the selected explicit format.'))
            typed.append(f'{date_value} AS {ref}')
        else:
            if field in IDENTIFIERS:
                numeric_form = f"regexp_full_match({ref}, '[+-]?[0-9]+([.][0-9]+)?([eE][+-]?[0-9]+)?') AND (contains({ref}, '.') OR contains(lower({ref}), 'e') OR starts_with({ref}, '-'))"
                expanded = f'expanded_{field}'
                safe = f"{expanded} IS NOT NULL AND try_cast({expanded} AS DECIMAL(38,15)) >= 0 AND try_cast({expanded} AS DECIMAL(38,15)) < 1000000000000000 AND regexp_full_match(split_part({expanded}, '.', 2), '0*')"
                errors.append((field, f'{numeric_form} AND NOT coalesce(({safe}), false)', 'POTENTIALLY_CORRUPTED_IDENTIFIER', 'Nonintegral or over-15-digit numeric notation; verify original identifier text.'))
                typed.append(f'CASE WHEN {numeric_form} AND ({safe}) THEN CAST(try_cast({expanded} AS DECIMAL(38,0)) AS VARCHAR) ELSE {ref} END AS {ref}')
                continue
            if field == 'CURRENCY':
                errors.append((field, f"{ref} IS NOT NULL AND NOT regexp_full_match({ref}, '[A-Z]{{3}}')", 'INVALID_CURRENCY', 'Currency must be a three-letter code.'))
            typed.append(ref)
    checks = ','.join(f'CASE WHEN {condition} THEN struct_pack(field := {quote(field)}, code := {quote(code)}, message := {quote(message)}) END' for field, condition, code, message in errors)
    raw_map = 'map([' + ','.join(quote(f) for f in FIELDS) + '],[' + ','.join('c.original."' + f + '"' for f in FIELDS) + '])'
    maximum = run.con.execute(f'SELECT max(ingest_id) FROM raw_{source}').fetchone()[0] or 0
    chunk = run.settings.normalization_chunk_rows
    observed_fractional_digits = 0
    created = False
    ranges = [(0, 0)] if maximum == 0 else ((lower, min(lower + chunk, maximum)) for lower in range(0, maximum, chunk))
    for lower, upper in ranges:
        run.con.execute(f'''CREATE OR REPLACE TEMP TABLE cleaned AS
            SELECT {','.join(clean)}, struct_pack({originals}) AS original,
                   source_file, source_sheet, source_row, native_numeric, ingest_id
            FROM raw_{source} WHERE ingest_id > ? AND ingest_id <= ?''', [lower, upper])
        updates = []
        for field in ('EVENT_BEGIN_TIME', 'EVENT_END_TIME'):
            updates.append(f'''"{field}" = CASE
                WHEN regexp_full_match("{field}", '[0-9]{{1,6}}([.]0+)?')
                    AND try_strptime(lpad(split_part("{field}",'.',1),6,'0'),'%H%M%S') IS NOT NULL
                    THEN strftime(try_strptime(lpad(split_part("{field}",'.',1),6,'0'),'%H%M%S'),'%H:%M:%S')
                WHEN regexp_full_match("{field}", '[0-9]{{1,2}}:[0-9]{{2}}:[0-9]{{2}}')
                    AND try_strptime("{field}",'%H:%M:%S') IS NOT NULL
                    THEN strftime(try_strptime("{field}",'%H:%M:%S'),'%H:%M:%S') ELSE "{field}" END''')
        for field in IDENTIFIERS:
            run.con.execute(f'ALTER TABLE cleaned ADD COLUMN expanded_{field} VARCHAR')
        # These assignments are independent and DuckDB evaluates them from the
        # pre-update row. Combining them avoids repeated scans of every chunk.
        run.con.execute('UPDATE cleaned SET ' + ','.join(updates + numeric_updates + identifier_updates))
        found_scale = run.con.execute(f'SELECT greatest({observed_scale}) FROM cleaned').fetchone()[0] or 0
        observed_fractional_digits = max(observed_fractional_digits, found_scale)
        # One validation scan per bounded chunk; raw text and provenance remain exact.
        run.con.execute(f'''INSERT INTO issues
            SELECT {quote(source)}, c.source_file, c.source_sheet, c.source_row,
                   problem.field, {raw_map}[problem.field], problem.code, problem.message
            FROM (SELECT *, unnest(list_filter([{checks}], x -> x IS NOT NULL)) AS problem FROM cleaned) c
            WHERE problem IS NOT NULL''')
        select = f'SELECT {",".join(typed)}, source_file, source_sheet, source_row, ingest_id::BIGINT AS record_id FROM cleaned'
        if not created:
            run.con.execute(f'CREATE TABLE {source} AS {select}')
            created = True
        else:
            run.con.execute(f'INSERT INTO {source} {select}')
        run.emit('normalization', source=source, records=upper)
        run.con.execute('DROP TABLE cleaned')
    run.meta.setdefault('decimal_requirements', {})[source] = {
        'observed_fractional_digits': observed_fractional_digits,
        'configured_scale': run.settings.decimal_scale,
    }
