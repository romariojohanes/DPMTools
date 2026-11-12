from datetime import datetime
from decimal import Decimal

import pytest
from openpyxl import Workbook

from core.engine import Run
from core.schema import FIELDS, InputOptions, Settings, resolve_inputs
from core.workflow import execute
from tools.generate_samples import cdr, write_csv, write_xlsx


def test_csv_xlsx_provenance_and_strings(run_pair):
    row = cdr(IMSI='000000000000001', ANUM='00001', BNUM='00002')
    run = run_pair(legacy=[row], saas=[row], xlsx=True)
    assert run.meta['result'] == 'MATCH', run.meta
    for source in ('legacy', 'saas'):
        data = run.rows(f'SELECT * FROM {source}')[0]
        assert data['source_row'] == 2
        assert data['IMSI'] == '000000000000001'
        assert data['GPRS_CHARGING_ID'] == '00000012345678901234'
        assert data['ANUM'] == '00001'
        assert data['BNUM'] == '00002'
        assert data['EVENT_BEGIN_TIME'] == '080000_RAW'
        assert data['EVENT_END_TIME'] == '080030_RAW'
        assert data['source_file'].endswith('.csv' if source == 'legacy' else '.xlsx')
    assert (run.directory / 'legacy.parquet').exists()


def test_multiple_files_and_directories(tmp_path):
    left = tmp_path / 'legacy'
    right = tmp_path / 'saas'
    for folder in (left, right):
        write_csv(folder / 'first.csv', [cdr()])
        write_xlsx(folder / 'second.xlsx', [cdr(HPMN='TST02')])
    path = execute([left], [right], root=tmp_path / 'runs', settings=Settings(memory_limit='256MB'))
    with Run(path) as run:
        assert run.meta['result'] == 'MATCH', run.meta
        assert len(run.meta['files']) == 4
        assert run.rows('SELECT count(*) AS n FROM legacy')[0]['n'] == 2


@pytest.mark.parametrize('value', ['invalid', '1.2345678901234', 'NaN', 'Inf', '1,000.00', '999999999999999999999999999999999999999999999', '', 'NULL'])
def test_invalid_financial_values_block_run(run_pair, value):
    run = run_pair(legacy=[cdr(RATED_AMOUNT_TAP=value)])
    assert run.meta['status'] == 'VALIDATION_FAILED', run.meta
    assert run.issue_count() >= 1
    assert 'result' not in run.meta
    row = run.rows("SELECT * FROM issues WHERE field='RATED_AMOUNT_TAP'")[0]
    assert row['source_row'] == 2
    assert row['raw_value'] == (value or None)
    assert run.con.execute("SELECT count(*) FROM information_schema.tables WHERE table_name='revenue'").fetchone()[0] == 0
    assert not (run.directory / 'legacy.parquet').exists()


def test_comma_decimal_and_encoding(tmp_path):
    row = cdr(RATED_AMOUNT_TAP='12,345678901234', RATE='0,0001', SERVICE='Données')
    left = write_csv(tmp_path / 'legacy.csv', [row], delimiter=';', encoding='cp1252')
    right = write_csv(tmp_path / 'saas.csv', [row], delimiter=';', encoding='cp1252')
    options = InputOptions(delimiter=';', decimal_separator=',', encoding='cp1252')
    path = execute([left], [right], legacy_options=options, saas_options=options, root=tmp_path / 'runs')
    with Run(path) as run:
        assert run.meta['result'] == 'MATCH', run.meta
        assert run.rows('SELECT * FROM revenue')[0]['legacy_amount'] == Decimal('12.345678901234')
        assert run.rows('SELECT SERVICE FROM legacy')[0]['SERVICE'] == 'Données'


def test_dot_is_rejected_when_comma_selected(run_pair):
    run = run_pair(legacy=[cdr(RATED_AMOUNT_TAP='12.25')], legacy_options=InputOptions(decimal_separator=','))
    assert run.meta['status'] == 'VALIDATION_FAILED'


def test_normalized_headers_nulls_operators_and_currency(tmp_path):
    row = cdr(HPMN=' tst01 ', VPMN='tst91', CURRENCY=' usd ', TAX_AMOUNT_TAP=' N/A ')
    remapped = {key.lower().replace('_', ' '): value for key, value in row.items()}
    left = write_csv(tmp_path / 'legacy.csv', [remapped], fields=list(remapped))
    right = write_csv(tmp_path / 'saas.csv', [cdr(TAX_AMOUNT_TAP='')])
    path = execute([left], [right], root=tmp_path / 'runs')
    with Run(path) as run:
        assert run.meta['result'] == 'MATCH', run.meta
        assert run.rows('SELECT TAX_AMOUNT_TAP FROM legacy')[0]['TAX_AMOUNT_TAP'] is None


def test_invalid_dates_currency_and_other_numeric(run_pair):
    run = run_pair(legacy=[cdr(EVENT_DATE='25/10/2026', CURRENCY='US', VOLUME_TOTAL='oops')])
    assert run.meta['status'] == 'VALIDATION_FAILED'
    assert {r['code'] for r in run.rows('SELECT * FROM issues')} >= {'INVALID_DATE', 'INVALID_CURRENCY', 'INVALID_DECIMAL'}


def test_explicit_date_and_traffic_constraint(run_pair):
    row = cdr(EVENT_DATE='25/10/2026')
    options = InputOptions(date_format='%d/%m/%Y')
    run = run_pair(legacy=[row], saas=[row], legacy_options=options, saas_options=options, traffic_date='2026-10-25')
    assert run.meta['result'] == 'MATCH', run.meta
    bad = run_pair(traffic_date='2026-10-24')
    assert bad.meta['status'] == 'VALIDATION_FAILED'
    assert all(r['code'] == 'TRAFFIC_DATE_MISMATCH' for r in bad.rows('SELECT * FROM issues'))


def test_unsafe_excel_identifiers_and_formulas(run_pair):
    row = cdr(IMSI=123456789012345, RATED_AMOUNT_TAP='=1+1')
    run = run_pair(saas=[row], xlsx=True)
    assert run.meta['status'] == 'VALIDATION_FAILED'
    assert len(run.rows("SELECT * FROM issues WHERE code='UNSAFE_EXCEL_CELL'")) == 1
    assert run.rows('SELECT IMSI FROM saas')[0]['IMSI'] == '123456789012345'


def test_xlsx_real_dates(run_pair):
    row = cdr(EVENT_DATE=datetime(2026, 10, 25))
    run = run_pair(saas=[row], xlsx=True)
    assert run.meta['result'] == 'MATCH', run.meta


@pytest.mark.parametrize('separator', ['.', ','])
def test_native_excel_small_numeric_amount(run_pair, separator):
    expected = '0.000000000001'
    run = run_pair(legacy=[cdr(RATED_AMOUNT_TAP=expected)],
                   saas=[cdr(RATED_AMOUNT_TAP=1e-12, RATE=0.0001)], xlsx=True,
                   saas_options=InputOptions(decimal_separator=separator))
    assert run.meta['result'] == 'MATCH', run.meta
    assert run.rows('SELECT * FROM revenue')[0]['saas_amount'] == Decimal(expected)


def test_malformed_csv_and_missing_header_are_reported(tmp_path):
    left = write_csv(tmp_path / 'legacy.csv', [cdr()])
    with left.open('a', encoding='utf-8') as handle:
        handle.write('too,few,columns\n')
    right = write_csv(tmp_path / 'saas.csv', [cdr()])
    path = execute([left], [right], root=tmp_path / 'runs')
    with Run(path) as run:
        assert run.meta['status'] == 'VALIDATION_FAILED', run.meta
        assert run.rows("SELECT * FROM issues WHERE code='FILE_ERROR'")
        assert run.con.execute('SELECT count(*) FROM raw_legacy').fetchone()[0] == 0


def test_missing_columns_and_duplicate_inputs(tmp_path):
    left = write_csv(tmp_path / 'legacy.csv', [{'IMSI': 'TEST'}], fields=['IMSI'])
    right = write_csv(tmp_path / 'saas.csv', [cdr()])
    path = execute([left], [right], root=tmp_path / 'runs')
    with Run(path) as run:
        assert run.meta['status'] == 'VALIDATION_FAILED'
        assert 'Missing required columns' in run.rows("SELECT * FROM issues WHERE code='FILE_ERROR'")[0]['message']
    with pytest.raises(ValueError, match='Duplicate'):
        resolve_inputs([left, left])
    with pytest.raises(ValueError, match='same file'):
        execute([right], [right])


def test_user_column_mapping_is_independent_and_audited(tmp_path):
    legacy_headers = {field: f'Legacy {field.lower()}' for field in FIELDS}
    saas_headers = {field: f'SaaS-{field.lower()}' for field in FIELDS}
    row = cdr()
    legacy_row = {legacy_headers[field]: row[field] for field in FIELDS}
    saas_row = {saas_headers[field]: row[field] for field in FIELDS}
    left = write_csv(tmp_path / 'mapped_legacy.csv', [legacy_row], fields=list(legacy_headers.values()))
    right = tmp_path / 'mapped_saas.xlsx'
    workbook = Workbook(write_only=True)
    sheet = workbook.create_sheet('CDR')
    sheet.append(list(saas_headers.values()))
    sheet.append([saas_row[saas_headers[field]] for field in FIELDS])
    workbook.save(right)
    legacy_mapping = {source: target for target, source in legacy_headers.items()}
    saas_mapping = {source: target for target, source in saas_headers.items()}
    path = execute([left], [right], root=tmp_path / 'mapped_runs',
                   legacy_options=InputOptions(column_mapping=legacy_mapping),
                   saas_options=InputOptions(column_mapping=saas_mapping),
                   settings=Settings(memory_limit='256MB', threads=2))
    with Run(path) as run:
        assert run.meta['status'] == 'COMPLETED', run.meta
        assert run.meta['result'] == 'MATCH'
        assert run.meta['options']['legacy']['column_mapping']['LEGACY_EVENT_DATE'] == 'EVENT_DATE'
        assert run.meta['options']['saas']['column_mapping']['SAAS_EVENT_DATE'] == 'EVENT_DATE'
        assert run.rows('SELECT IMSI,source_row FROM legacy') == [
            {'IMSI': row['IMSI'], 'source_row': 2}]


def test_column_mapping_rejects_conflicts_and_missing_targets(tmp_path):
    with pytest.raises(ValueError, match='at most one'):
        InputOptions(column_mapping={'first': 'IMSI', 'second': 'IMSI'})
    left = write_csv(tmp_path / 'partial.csv', [{'Subscriber': '123'}], fields=['Subscriber'])
    right = write_csv(tmp_path / 'canonical.csv', [cdr()])
    path = execute([left], [right], root=tmp_path / 'partial_runs',
                   legacy_options=InputOptions(column_mapping={'Subscriber': 'IMSI'}),
                   settings=Settings(memory_limit='256MB', threads=2))
    with Run(path) as run:
        assert run.meta['status'] == 'VALIDATION_FAILED'
        assert 'Missing required columns' in run.rows(
            "SELECT message FROM issues WHERE code='FILE_ERROR'")[0]['message']


def test_empty_file_is_not_a_zero_value_match(run_pair):
    run = run_pair(legacy=[])
    assert run.meta['status'] == 'VALIDATION_FAILED'
    assert run.rows("SELECT * FROM issues WHERE code='EMPTY_FILE'")


def test_multiple_sheets_require_selection(tmp_path):
    left = write_csv(tmp_path / 'legacy.csv', [cdr()])
    workbook = Workbook()
    workbook.active.title = 'CDR'
    workbook.active.append(FIELDS)
    workbook.active.append([cdr()[f] for f in FIELDS])
    workbook.create_sheet('Other')
    right = tmp_path / 'saas.xlsx'
    workbook.save(right)
    path = execute([left], [right], root=tmp_path / 'bad')
    with Run(path) as run:
        assert run.meta['status'] == 'VALIDATION_FAILED'
    path = execute([left], [right], saas_options=InputOptions(sheet_name='CDR'), root=tmp_path / 'good')
    with Run(path) as run:
        assert run.meta['result'] == 'MATCH', run.meta


def test_csv_physical_lines_with_blanks_and_multiline_fields(tmp_path):
    left = write_csv(tmp_path / 'legacy.csv', [cdr(SERVICE='DATA\nMULTILINE'), cdr()])
    text = left.read_text(encoding='utf-8-sig')
    header, rest = text.split('\n', 1)
    left.write_text(header + '\n\n' + rest, encoding='utf-8-sig')
    right = write_csv(tmp_path / 'saas.csv', [cdr(SERVICE='DATA\nMULTILINE'), cdr()])
    path = execute([left], [right], root=tmp_path / 'runs')
    with Run(path) as run:
        assert run.meta['aggregate_result'] == 'MATCH', run.meta
        assert run.meta['result'] == 'MISMATCH'  # identical duplicate keys remain unresolved
        assert [r['source_row'] for r in run.rows('SELECT source_row FROM legacy ORDER BY source_row')] == [3, 5]


@pytest.mark.parametrize('value,expected', [(123456789012345, '123456789012345'), (123.0, '123'), ('1.2345E+4', '12345')])
def test_safe_excel_numeric_identifier(run_pair, value, expected):
    run = run_pair(legacy=[cdr(IMSI=expected)], saas=[cdr(IMSI=value)], xlsx=True)
    assert run.meta['status'] == 'COMPLETED', run.meta
    assert run.rows('SELECT IMSI FROM saas')[0]['IMSI'] == expected


@pytest.mark.parametrize('value', [1234567890123456, 12.5, -5, True, '1.234567890123456E+15'])
def test_potentially_corrupted_identifiers_block(run_pair, value):
    run = run_pair(saas=[cdr(IMSI=value)], xlsx=True)
    assert run.meta['status'] == 'VALIDATION_FAILED', run.meta
    assert run.rows("SELECT * FROM issues WHERE code='POTENTIALLY_CORRUPTED_IDENTIFIER'")


def test_scientific_financial_and_extended_precision(run_pair):
    row = cdr(RATED_AMOUNT_TAP='1.23456789012345678901e-2')
    run = run_pair(legacy=[row], saas=[row], settings=Settings(memory_limit='256MB', decimal_scale=24))
    assert run.meta['status'] == 'COMPLETED', run.meta
    assert run.rows('SELECT legacy_amount FROM revenue')[0]['legacy_amount'] == Decimal('0.0123456789012345678901')


def test_month_first_date(run_pair):
    run = run_pair(legacy=[cdr(EVENT_DATE='04/01/2026')], saas=[cdr(EVENT_DATE='04/01/2026')])
    assert str(run.rows('SELECT EVENT_DATE FROM legacy')[0]['EVENT_DATE']) == '2026-04-01'
