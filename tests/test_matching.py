from decimal import Decimal

import pytest

from core.engine import Run
from core.schema import Settings
from core.workflow import execute
from tools.generate_samples import cdr, write_csv, write_xlsx


@pytest.mark.parametrize('left_format,right_format', [('csv', 'csv'), ('xlsx', 'xlsx'), ('xlsx', 'csv'), ('csv', 'xlsx')])
def test_all_formats_and_reordered_sessions(tmp_path, left_format, right_format):
    rows = [cdr(GPRS_CHARGING_ID=str(i), RATED_AMOUNT_TAP=str(i)) for i in range(5)]
    writers = {'csv': write_csv, 'xlsx': write_xlsx}
    left = writers[left_format](tmp_path / ('legacy.' + left_format), rows)
    right = writers[right_format](tmp_path / ('saas.' + right_format), reversed(rows))
    path = execute([left], [right], root=tmp_path / 'runs', settings=Settings(memory_limit='256MB', threads=2))
    with Run(path) as run:
        assert run.meta['status'] == 'COMPLETED', run.meta
        assert run.meta['result'] == 'MATCH'
        assert run.con.execute("SELECT count(*) FROM cdr_matches WHERE status='MATCHED_SAME_RATING'").fetchone()[0] == 5
        row = run.rows('SELECT * FROM cdr_details WHERE legacy_row=2')[0]
        assert row['saas_row'] == 6
        assert row['legacy_file'].endswith(left_format)
        assert row['saas_file'].endswith(right_format)


def test_attribution_all_components_and_source_references(run_pair):
    left = [cdr(GPRS_CHARGING_ID='A', RATED_AMOUNT_TAP='2'), cdr(GPRS_CHARGING_ID='B', RATED_AMOUNT_TAP='4'),
            cdr(GPRS_CHARGING_ID='D', RATED_AMOUNT_TAP='3'), cdr(GPRS_CHARGING_ID='D', RATED_AMOUNT_TAP='2')]
    right = [cdr(GPRS_CHARGING_ID='C', RATED_AMOUNT_TAP='7'), cdr(GPRS_CHARGING_ID='A', RATED_AMOUNT_TAP='5'),
             cdr(GPRS_CHARGING_ID='D', RATED_AMOUNT_TAP='9')]
    run = run_pair(legacy=left, saas=right)
    assert run.meta['status'] == 'COMPLETED', run.meta
    a = run.rows('SELECT * FROM revenue_attribution')[0]
    assert (a['matched_rating_differences'], a['saas_only_amount'], a['negative_legacy_only_amount'], a['unresolved_ambiguous_difference']) == (3, 7, -4, 4)
    assert a['total_variance'] == a['explained_variance'] == 10
    assert a['residual'] == 0
    diff = run.rows("SELECT * FROM field_differences WHERE field='RATED_AMOUNT_TAP'")[0]
    assert (diff['legacy_row'], diff['saas_row'], diff['difference']) == (2, 3, 3)
    assert len(run.rows('SELECT * FROM ambiguous_records')) == 3
    assert run.rows("SELECT * FROM cdr_matches WHERE status='DUPLICATE_OR_AMBIGUOUS'")[0]['legacy_id'] is None


def test_equal_revenue_different_populations(run_pair):
    run = run_pair(legacy=[cdr(GPRS_CHARGING_ID='LEFT')], saas=[cdr(GPRS_CHARGING_ID='RIGHT')])
    assert run.meta['aggregate_result'] == 'MATCH'
    assert run.meta['result'] == 'MISMATCH'
    assert {r['status'] for r in run.rows('SELECT * FROM matching_summary')} == {'LEGACY_ONLY', 'SAAS_ONLY'}
    assert run.rows('SELECT residual FROM revenue_attribution')[0]['residual'] == 0


def test_duplicate_keys_on_both_sides_never_paired(run_pair):
    run = run_pair(legacy=[cdr(), cdr()], saas=[cdr(), cdr()])
    assert len(run.rows('SELECT * FROM cdr_matches')) == 1
    assert len(run.rows('SELECT * FROM ambiguous_records')) == 4
    assert not run.rows('SELECT * FROM field_differences')
    assert run.meta['result'] == 'MISMATCH'


def test_incomplete_and_unknown_service_are_ambiguous(run_pair):
    rows = [cdr(IMSI=''), cdr(SERVICE_TYPE='UNKNOWN')]
    run = run_pair(legacy=rows, saas=rows)
    assert all(r['status'] == 'DUPLICATE_OR_AMBIGUOUS' for r in run.rows('SELECT * FROM cdr_matches'))
    assert all(r['reason'] == 'INCOMPLETE_OR_UNSUPPORTED_KEY' for r in run.rows('SELECT * FROM ambiguous_records'))


def test_raw_format_differences_and_secondary_values(run_pair):
    run = run_pair(legacy=[cdr(RATED_AMOUNT_TAP='1.0', RATE='0.5')], saas=[cdr(RATED_AMOUNT_TAP='1.00', RATE='0.6')])
    assert run.rows('SELECT status FROM cdr_matches')[0]['status'] == 'MATCHED_SAME_RATING'
    diffs = {r['field']: r for r in run.rows('SELECT * FROM field_differences')}
    assert diffs['RATED_AMOUNT_TAP']['difference_type'] == 'RAW_FORMAT_ONLY'
    assert diffs['RATE']['difference_type'] == 'NORMALIZED_VALUE'
    assert diffs['RATE']['difference'] == Decimal('0.1')
    assert run.meta['result'] == 'MISMATCH'


def test_currency_change_attribution_is_separate(run_pair):
    run = run_pair(saas=[cdr(CURRENCY='IDR')])
    rows = run.rows('SELECT * FROM revenue_attribution ORDER BY CURRENCY')
    assert len(rows) == 2
    assert all(r['residual'] == 0 for r in rows)
    assert run.rows('SELECT rating_difference FROM cdr_details')[0]['rating_difference'] is None
    assert run.rows("SELECT difference FROM field_differences WHERE field='RATED_AMOUNT_TAP'") == []


def test_full_field_audit_and_custom_profiles(run_pair):
    run = run_pair(saas=[cdr(SERVICE='changed')], full_audit=True)
    assert run.rows("SELECT field FROM field_differences WHERE field='SERVICE'")
    for service in ('VOICE', 'SMS'):
        run = run_pair(legacy=[cdr(SERVICE_TYPE=service)], saas=[cdr(SERVICE_TYPE=service)])
        assert run.meta['result'] == 'MATCH'
    run = run_pair(legacy=[cdr(GPRS_CHARGING_ID='1')], saas=[cdr(GPRS_CHARGING_ID='2')], matching_profiles={'GPRS': ['IMSI', 'EVENT_DATE']})
    assert run.rows('SELECT status FROM cdr_matches')[0]['status'] == 'MATCHED_SAME_RATING'
    with pytest.raises(ValueError, match='non-rating'):
        run_pair(matching_profiles={'GPRS': ['IMSI', 'RATED_AMOUNT_TAP']})


def test_extra_columns_and_paginated_details(tmp_path):
    left = write_csv(tmp_path / 'left.csv', [dict(cdr(), EXTRA='ignored')], fields=list(cdr()) + ['EXTRA'])
    right = write_csv(tmp_path / 'right.csv', [cdr(RATED_AMOUNT_TAP='3')])
    with Run(execute([left], [right], root=tmp_path / 'runs')) as run:
        rows, total = run.table_page('rating_differences', filters={'HPMN': 'TST01'}, limit=1)
        assert total == 1
        assert rows[0]['rating_difference'] == Decimal('1.999999999999')



def test_directional_voice_sms_profiles(run_pair):
    for service in ('VOICE-MO', 'VOICE-MT', 'SMS-MO', 'SMS-MT'):
        values = {'SERVICE_TYPE': service, 'ANUM': '' if service.endswith('MT') else '000123',
                  'BNUM': '000456' if service.endswith('MT') else ''}
        run = run_pair(legacy=[cdr(**values)], saas=[cdr(**values)])
        assert run.meta['result'] == 'MATCH', run.meta


def test_key_serialization_cannot_collide_on_delimiters(run_pair):
    left = cdr(IMSI='a|b', GPRS_CHARGING_ID='c')
    right = cdr(IMSI='a', GPRS_CHARGING_ID='b|c')
    run = run_pair(legacy=[left], saas=[right], matching_profiles={'GPRS': ['IMSI', 'GPRS_CHARGING_ID']})
    assert {r['status'] for r in run.rows('SELECT status FROM cdr_matches')} == {'LEGACY_ONLY', 'SAAS_ONLY'}



def test_null_profile_database_filter(run_pair):
    run = run_pair(legacy=[cdr(SERVICE_TYPE='')], saas=[cdr(SERVICE_TYPE='')])
    rows, total = run.table_page('matching_summary', filters={'profile': None})
    assert total == 1
    assert rows[0]['status'] == 'DUPLICATE_OR_AMBIGUOUS'


def test_ambiguous_members_are_materialized_without_cartesian_expansion(run_pair):
    duplicate = cdr(IMSI='duplicate')
    unique = cdr(IMSI='unique')
    run = run_pair(legacy=[duplicate, duplicate, unique], saas=[duplicate, duplicate, duplicate, unique])
    assert run.con.execute('SELECT count(*) FROM ambiguous_members').fetchone()[0] == 5
    records = run.rows('''SELECT source,record_id,source_row,match_key FROM ambiguous_records
                          ORDER BY source,match_key,record_id''')
    assert len(records) == 5
    assert len({(row['source'], row['record_id']) for row in records}) == 5
    assert {row['source'] for row in records} == {'legacy', 'saas'}
    assert run.rows("SELECT status,legacy_count,saas_count FROM cdr_matches WHERE status='DUPLICATE_OR_AMBIGUOUS'") == [
        {'status': 'DUPLICATE_OR_AMBIGUOUS', 'legacy_count': 2, 'saas_count': 3}]
    assert run.rows('SELECT residual,status FROM revenue_attribution') == [
        {'residual': 0, 'status': 'MATCH'}]
