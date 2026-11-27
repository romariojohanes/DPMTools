import csv
from decimal import Decimal

from openpyxl import load_workbook
import duckdb
import pytest

from core.engine import Run
from core.reporting import export_all_records, export_all_records_xlsx, export_detail
from core.schema import Settings
from core.variance import enhanced_query, percentage_sql, register_percentage_function
from tools.generate_samples import cdr


@pytest.mark.parametrize('numerator,denominator,input_scale,expected', [
    ('0.000000000000000000000001', '0.000000000000000000000003', 24, '33.333333333333'),
    ('12345678901234567890.123456789012', '100', 12, '12345678901234567890.123456789012'),
    ('1', '-2', 24, '50.000000000000'),
    ('0.000000000000005', '1', 24, '0.000000000000'),
    ('0.000000000000015', '1', 24, '0.000000000002'),
    ('1', '0', 24, None),
    (None, '1', 24, None),
    ('1', None, 24, None),
])
def test_percentage_sql_preserves_original_decimal_precision(numerator, denominator, input_scale, expected):
    with duckdb.connect() as connection:
        register_percentage_function(connection)
        query = percentage_sql('n', 'd')
        value, data_type = connection.execute(f'''SELECT {query}, typeof({query}) FROM
            (SELECT CAST(? AS DECIMAL(38,{input_scale})) AS n,
                    CAST(? AS DECIMAL(38,{input_scale})) AS d)''', [numerator, denominator]).fetchone()
    assert value == (Decimal(expected) if expected is not None else None)
    assert data_type == 'DECIMAL(38,12)'


def test_saved_variance_view_survives_other_connection_closing_and_reopen(tmp_path):
    owner = Run.create(tmp_path / 'runs', Settings(memory_limit='128MB', threads=1))
    directory = owner.directory
    query = percentage_sql('1::DECIMAL(38,24)', '3::DECIMAL(38,24)')
    owner.con.execute(f'CREATE VIEW percentage_probe AS SELECT {query} AS variance')
    viewer = Run(directory)
    try:
        owner.close()
        assert viewer.con.execute('SELECT * FROM percentage_probe').fetchone() == (Decimal('33.333333333333'),)
    finally:
        viewer.close()
    with Run(directory) as reopened:
        assert reopened.con.execute('SELECT * FROM percentage_probe').fetchone() == (Decimal('33.333333333333'),)


def test_cdr_variance_positive_negative_zero_and_zero_baseline(run_pair):
    legacy = [cdr(IMSI=str(i), GPRS_CHARGING_ID=str(i), RATED_AMOUNT_TAP=value)
              for i, value in enumerate(('10', '10', '10', '0'), 1)]
    saas = [cdr(IMSI=str(i), GPRS_CHARGING_ID=str(i), RATED_AMOUNT_TAP=value)
            for i, value in enumerate(('12.5', '7.5', '10', '1'), 1)]
    run = run_pair(legacy=legacy, saas=saas)
    rows = run.rows('SELECT rating_variance_amount,rating_variance_pct FROM cdr_details ORDER BY legacy_id')
    assert rows == [
        {'rating_variance_amount': Decimal('2.5'), 'rating_variance_pct': Decimal('25')},
        {'rating_variance_amount': Decimal('-2.5'), 'rating_variance_pct': Decimal('-25')},
        {'rating_variance_amount': Decimal('0'), 'rating_variance_pct': Decimal('0')},
        {'rating_variance_amount': Decimal('1'), 'rating_variance_pct': None},
    ]


def test_cdr_variance_is_null_for_currency_mismatch_unmatched_and_ambiguous(run_pair):
    duplicate = cdr(IMSI='DUP', GPRS_CHARGING_ID='DUP')
    run = run_pair(
        legacy=[cdr(IMSI='FX', GPRS_CHARGING_ID='FX', CURRENCY='USD'),
                cdr(IMSI='LEFT', GPRS_CHARGING_ID='LEFT'), duplicate, duplicate],
        saas=[cdr(IMSI='FX', GPRS_CHARGING_ID='FX', CURRENCY='EUR'),
              cdr(IMSI='RIGHT', GPRS_CHARGING_ID='RIGHT'), duplicate, duplicate])
    rows = run.rows('SELECT status,rating_variance_pct FROM cdr_details ORDER BY status')
    assert rows and all(row['rating_variance_pct'] is None for row in rows)


def test_aggregate_exact_precision_and_historical_projection(run_pair):
    run = run_pair(legacy=[cdr(RATED_AMOUNT_TAP='3')], saas=[cdr(RATED_AMOUNT_TAP='4')])
    assert run.rows('SELECT revenue_variance_pct FROM revenue')[0]['revenue_variance_pct'] == Decimal('33.333333333333')
    run.con.execute('CREATE TABLE historical_hpmn AS SELECT * EXCLUDE(revenue_variance_pct) FROM hpmn')
    query = enhanced_query(run, 'historical_hpmn')
    # Unknown tables remain unchanged; historical report tables are projected by name.
    assert 'revenue_variance_pct' not in [c[0] for c in run.con.execute(f'DESCRIBE ({query})').fetchall()]
    run.con.execute('CREATE OR REPLACE TABLE hpmn AS SELECT * EXCLUDE(revenue_variance_pct) FROM hpmn')
    assert run.con.execute(f'SELECT revenue_variance_pct FROM ({enhanced_query(run, "hpmn")})').fetchone()[0] == Decimal('33.333333333333')


def test_variance_columns_in_streaming_csv_xlsx_and_detail_exports(run_pair, tmp_path):
    run = run_pair(legacy=[cdr(RATED_AMOUNT_TAP='8')], saas=[cdr(RATED_AMOUNT_TAP='10')])
    csv_path, count, _ = export_all_records(run, 'csv', tmp_path)
    with csv_path.open(encoding='utf-8', newline='') as handle:
        rows = list(csv.DictReader(handle, delimiter=';'))
    assert count == 1 and rows[0]['rating_variance_pct'] == '25.000000000000'
    xlsx_path, count, _ = export_all_records_xlsx(run, tmp_path)
    book = load_workbook(xlsx_path, read_only=True)
    try:
        headers = [cell.value for cell in next(book['CDR Matching Details'].iter_rows())]
        assert {'rating_variance_amount', 'rating_variance_pct'} <= set(headers)
    finally:
        book.close()
    detail = export_detail(run, 'rating_differences', 'csv', tmp_path)
    assert 'rating_variance_pct' in detail.read_text(encoding='utf-8').splitlines()[0]


def test_large_csv_export_has_no_match_key_sort():
    from pathlib import Path
    source = (Path(__file__).parents[1] / 'core' / 'reporting.py').read_text(encoding='utf-8')
    assert 'ORDER BY match_key' not in source
