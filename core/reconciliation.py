"""Seven aggregate levels. No individual CDR matching or duplicate elimination."""


def _comparison(run, table, keys):
    group = ','.join(keys)
    select_keys = ','.join(f'coalesce(l.{key}, s.{key}) AS {key}' for key in keys)
    join = ' AND '.join(f'l.{key}=s.{key}' for key in keys)
    run.con.execute(f'''CREATE TABLE {table} AS
        WITH l AS (SELECT {group}, count(*) AS cdr_count, sum(RATED_AMOUNT_TAP) AS amount FROM legacy GROUP BY {group}),
             s AS (SELECT {group}, count(*) AS cdr_count, sum(RATED_AMOUNT_TAP) AS amount FROM saas GROUP BY {group})
        SELECT {select_keys},
            coalesce(l.cdr_count,0) AS legacy_count, coalesce(s.cdr_count,0) AS saas_count,
            abs(coalesce(s.cdr_count,0)-coalesce(l.cdr_count,0)) AS count_abs_variance,
            coalesce(l.amount,0) AS legacy_amount, coalesce(s.amount,0) AS saas_amount,
            coalesce(s.amount,0)-coalesce(l.amount,0) AS amount_variance,
            abs(coalesce(s.amount,0)-coalesce(l.amount,0)) AS amount_abs_variance,
            CASE WHEN l.cdr_count IS NULL THEN 'SAAS_ONLY' WHEN s.cdr_count IS NULL THEN 'LEGACY_ONLY' ELSE 'BOTH' END AS presence,
            CASE WHEN l.amount=s.amount THEN 'MATCH' ELSE 'MISMATCH' END AS revenue_status,
            CASE WHEN l.cdr_count=s.cdr_count THEN 'MATCH' ELSE 'MISMATCH' END AS count_status,
            CASE WHEN l.cdr_count=s.cdr_count AND l.amount=s.amount THEN 'MATCH' ELSE 'MISMATCH' END AS status
        FROM l FULL OUTER JOIN s ON {join}''')


def reconcile(run):
    if run.issue_count():
        raise ValueError('Validation errors must be resolved before reconciliation.')
    _comparison(run, 'revenue', ['CURRENCY'])
    _comparison(run, 'hpmn', ['HPMN', 'CURRENCY'])
    _comparison(run, 'vpmn', ['VPMN', 'CURRENCY'])
    _comparison(run, 'pairs', ['HPMN', 'VPMN', 'CURRENCY'])
    for operator in ('HPMN', 'VPMN'):
        run.con.execute(f'''CREATE TABLE {operator.lower()}_set AS
            WITH l AS (SELECT DISTINCT {operator} AS operator FROM legacy),
                 s AS (SELECT DISTINCT {operator} AS operator FROM saas)
            SELECT coalesce(l.operator,s.operator) AS operator,
                l.operator IS NOT NULL AS in_legacy, s.operator IS NOT NULL AS in_saas,
                CASE WHEN l.operator IS NULL THEN 'SAAS_ONLY' WHEN s.operator IS NULL THEN 'LEGACY_ONLY' ELSE 'BOTH' END AS presence,
                CASE WHEN l.operator IS NOT NULL AND s.operator IS NOT NULL THEN 'MATCH' ELSE 'MISMATCH' END AS status
            FROM l FULL OUTER JOIN s ON l.operator=s.operator''')
    run.con.execute('''CREATE TABLE counts AS
        WITH l AS (SELECT count(*) AS cdr, count(DISTINCT HPMN) AS hpmn, count(DISTINCT VPMN) AS vpmn FROM legacy),
             s AS (SELECT count(*) AS cdr, count(DISTINCT HPMN) AS hpmn, count(DISTINCT VPMN) AS vpmn FROM saas)
        SELECT 'CDR_COUNT' AS metric, l.cdr AS legacy_value, s.cdr AS saas_value,
            abs(s.cdr-l.cdr) AS abs_variance, CASE WHEN l.cdr=s.cdr THEN 'MATCH' ELSE 'MISMATCH' END AS status FROM l,s
        UNION ALL SELECT 'DISTINCT_HPMN', l.hpmn, s.hpmn, abs(s.hpmn-l.hpmn), CASE WHEN l.hpmn=s.hpmn THEN 'MATCH' ELSE 'MISMATCH' END FROM l,s
        UNION ALL SELECT 'DISTINCT_VPMN', l.vpmn, s.vpmn, abs(s.vpmn-l.vpmn), CASE WHEN l.vpmn=s.vpmn THEN 'MATCH' ELSE 'MISMATCH' END FROM l,s''')
    run.meta['result'] = 'MISMATCH' if any(run.con.execute(f"SELECT count(*) FROM {table} WHERE status <> 'MATCH'").fetchone()[0] for table in ('revenue', 'counts', 'hpmn_set', 'vpmn_set', 'hpmn', 'vpmn', 'pairs')) else 'MATCH'
