from decimal import Decimal

import pytest

from tools.generate_samples import cdr


def test_matching_revenue_and_counts(run_pair):
    run = run_pair()
    assert run.meta['status'] == 'COMPLETED', run.meta
    assert run.meta['result'] == 'MATCH'
    row = run.rows('SELECT * FROM revenue')[0]
    assert row['legacy_amount'] == Decimal('1.000000000001')
    assert row['amount_abs_variance'] == Decimal('0')
    assert row['revenue_status'] == 'MATCH'
    assert run.rows("SELECT * FROM counts WHERE metric='CDR_COUNT'")[0]['status'] == 'MATCH'


def test_revenue_variance(run_pair):
    run = run_pair(saas=[cdr(RATED_AMOUNT_TAP='1.000000000002')])
    row = run.rows('SELECT * FROM revenue')[0]
    assert row['amount_abs_variance'] == Decimal('0.000000000001')
    assert row['amount_variance'] == Decimal('0.000000000001')
    assert row['status'] == 'MISMATCH'


def test_different_counts_even_with_matching_revenue(run_pair):
    run = run_pair(legacy=[cdr(RATED_AMOUNT_TAP='2')], saas=[cdr(RATED_AMOUNT_TAP='1'), cdr(RATED_AMOUNT_TAP='1')])
    row = run.rows('SELECT * FROM revenue')[0]
    assert row['revenue_status'] == 'MATCH'
    assert row['count_status'] == 'MISMATCH'
    row = run.rows("SELECT * FROM counts WHERE metric='CDR_COUNT'")[0]
    assert (row['legacy_value'], row['saas_value'], row['abs_variance']) == (1, 2, 1)


@pytest.mark.parametrize('field,table', [('HPMN', 'hpmn_set'), ('VPMN', 'vpmn_set')])
def test_missing_operator(run_pair, field, table):
    run = run_pair(legacy=[cdr(), cdr(**{field: 'TST99'})])
    row = run.rows(f"SELECT * FROM {table} WHERE operator='TST99'")[0]
    assert row['presence'] == 'LEGACY_ONLY'
    assert row['status'] == 'MISMATCH'


def test_additional_hpmn_in_saas(run_pair):
    run = run_pair(saas=[cdr(), cdr(HPMN='TST03')])
    assert run.rows("SELECT * FROM hpmn WHERE HPMN='TST03'")[0]['presence'] == 'SAAS_ONLY'


@pytest.mark.parametrize('field,table', [('HPMN', 'hpmn_set'), ('VPMN', 'vpmn_set')])
def test_same_distinct_count_different_populations(run_pair, field, table):
    run = run_pair(saas=[cdr(**{field: 'TST99'})])
    count = run.rows(f"SELECT * FROM counts WHERE metric='DISTINCT_{field}'")[0]
    assert count['status'] == 'MATCH'
    assert len(run.rows(f"SELECT * FROM {table} WHERE status='MISMATCH'")) == 2
    assert run.meta['result'] == 'MISMATCH'


def test_decimal_precision_and_negative_values(run_pair):
    rows = [cdr(RATED_AMOUNT_TAP='99999999999999999999.123456789012'), cdr(RATED_AMOUNT_TAP='-0.000000000001')]
    run = run_pair(legacy=rows, saas=rows)
    assert run.rows('SELECT * FROM revenue')[0]['legacy_amount'] == Decimal('99999999999999999999.123456789011')


def test_currency_isolation_and_full_outer_join(run_pair):
    run = run_pair(legacy=[cdr(RATED_AMOUNT_TAP='0')], saas=[cdr(CURRENCY='IDR', RATED_AMOUNT_TAP='0')])
    rows = run.rows('SELECT * FROM revenue ORDER BY CURRENCY')
    assert len(rows) == 2
    assert {r['presence'] for r in rows} == {'LEGACY_ONLY', 'SAAS_ONLY'}
    assert all(row['status'] == 'MISMATCH' for row in rows)
    assert len(run.rows('SELECT * FROM pairs')) == 2


def test_pair_distribution_variance_with_matching_operator_totals(run_pair):
    left = [cdr(HPMN='TST01', VPMN='TST91'), cdr(HPMN='TST02', VPMN='TST92')]
    right = [cdr(HPMN='TST01', VPMN='TST92'), cdr(HPMN='TST02', VPMN='TST91')]
    run = run_pair(legacy=left, saas=right)
    assert all(r['status'] == 'MATCH' for r in run.rows('SELECT * FROM hpmn'))
    assert all(r['status'] == 'MATCH' for r in run.rows('SELECT * FROM vpmn'))
    assert all(r['status'] == 'MISMATCH' for r in run.rows('SELECT * FROM pairs'))
    assert run.meta['result'] == 'MISMATCH'
