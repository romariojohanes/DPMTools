"""Whole-dataset matching oracles, independent of normalization chunk layout.

The populated 30-chunk regression uses the supported 10k chunk size. A separate
sparse-ID regression exercises chunk 30 at the actual 250k default without
pretending that a small fixture certifies 7.5 million-row capacity.
"""
import json
from decimal import Decimal, localcontext
from random import Random

import core.ingestion as ingestion
from core.engine import Run
from core.schema import FIELDS, Settings
from core.workflow import execute
from tools.generate_samples import cdr, write_csv


AMOUNT = '1.000000000000000000000001'
CHANGED_AMOUNT = '1.000000000000000000000002'
EPSILON = Decimal('0.000000000000000000000001')
SAME = 'MATCHED_SAME_RATING'
DIFFERENT = 'MATCHED_DIFFERENT_RATING'
AMBIGUOUS = 'DUPLICATE_OR_AMBIGUOUS'


def _row(source, label, amount=AMOUNT, **overrides):
    return cdr(IMSI='000000000000001', GPRS_CHARGING_ID=label,
               EVENT_BEGIN_TIME='080000' if source == 'legacy' else '08:00:00',
               EVENT_END_TIME='080030' if source == 'legacy' else '08:00:30',
               RATED_AMOUNT_TAP=amount, RATE=str(EPSILON), **overrides)


def _cases():
    # Amounts deliberately tempt a first-row/closest-rating duplicate pairing.
    specifications = [
        ('RATING_CHANGE', (AMOUNT,), (CHANGED_AMOUNT,), DIFFERENT, {}, {}),
        ('RATE_CHANGE', (AMOUNT,), (AMOUNT,), SAME, {}, {'RATE': '0.000000000000000000000002'}),
        ('FORMAT_ONLY', ('1.0',), ('1.00',), SAME, {}, {}),
        ('FIELD_CHANGE', (AMOUNT,), (AMOUNT,), SAME, {'SERVICE': 'ORIGINAL\nLINE'}, {'SERVICE': 'UPDATED\nLINE'}),
        ('CURRENCY_CHANGE', ('2',), ('2',), DIFFERENT, {}, {'CURRENCY': 'IDR'}),
        ('LEFT_ONLY', ('4',), (), 'LEGACY_ONLY', {}, {}),
        ('RIGHT_ONLY', (), ('7',), 'SAAS_ONLY', {}, {}),
        ('DUP_LEFT', ('1', '9'), ('1',), AMBIGUOUS, {}, {}),
        ('DUP_RIGHT', ('4',), ('4', '5'), AMBIGUOUS, {}, {}),
        ('DUP_BOTH', ('2', '2'), ('2', '2', '4'), AMBIGUOUS, {}, {}),
        ('DUP_LEFT_ONLY', ('6', '7'), (), AMBIGUOUS, {}, {}),
        ('DUP_RIGHT_ONLY', (), ('8', '9'), AMBIGUOUS, {}, {}),
        ('INCOMPLETE', (AMOUNT,), (AMOUNT,), AMBIGUOUS, {'IMSI': ''}, {'IMSI': ''}),
        ('UNSUPPORTED', (AMOUNT,), (AMOUNT,), AMBIGUOUS, {'SERVICE_TYPE': 'UNKNOWN'}, {'SERVICE_TYPE': 'UNKNOWN'}),
        ('KEY_IMSI_A', ('2',), ('2',), SAME,
         {'GPRS_CHARGING_ID': 'SAME_CHARGING_ID'}, {'GPRS_CHARGING_ID': 'SAME_CHARGING_ID'}),
        ('KEY_IMSI_B', ('9',), ('9',), SAME,
         {'GPRS_CHARGING_ID': 'SAME_CHARGING_ID', 'IMSI': '000000000000002'},
         {'GPRS_CHARGING_ID': 'SAME_CHARGING_ID', 'IMSI': '000000000000002'}),
    ]
    rows = {'legacy': [], 'saas': []}
    expected = []
    for label, left, right, status, left_options, right_options in specifications:
        expected.append((label, status, len(left), len(right)))
        for source, amounts, options in (('legacy', left, left_options), ('saas', right, right_options)):
            for amount in amounts:
                # Merge overrides before calling cdr, including key-field changes.
                row = _row(source, label, amount)
                row.update(options)
                rows[source].append(row)
    return rows, expected


def _inputs(root, common, *, seed=None, split=False, distant=False):
    special, expected = _cases()
    paths = {}
    for side, source in enumerate(('legacy', 'saas')):
        order = list(range(common + len(special[source])))
        if seed is not None:
            Random(seed + side).shuffle(order)
        if distant:
            if source == 'legacy':
                duplicate = [common + i for i, row in enumerate(special[source]) if row['GPRS_CHARGING_ID'] == 'DUP_LEFT']
                forced = {0: 0, 1: duplicate[0], 299_999: duplicate[1]}
            else:
                duplicate = [common + i for i, row in enumerate(special[source]) if row['GPRS_CHARGING_ID'] == 'DUP_RIGHT']
                forced = {0: duplicate[0], 299_998: duplicate[1], 299_999: 0}
            used = set(forced.values())
            remaining = iter(value for value in order if value not in used)
            order = [forced[index] if index in forced else next(remaining) for index in range(len(order))]
        parts = (2 if source == 'legacy' else 3) if split else 1
        paths[source] = []
        for part in range(parts):
            selected = order[len(order) * part // parts:len(order) * (part + 1) // parts]
            rows = (_row(source, f'CDR_{index:07d}') if index < common else special[source][index - common]
                    for index in selected)
            paths[source].append(write_csv(root / source / f'part_{part}.csv', rows))
    return paths, expected


def _assert_global_oracle(run, common, expected):
    assert run.meta['status'] == 'COMPLETED', run.meta
    assert run.meta['result'] == 'MISMATCH'  # Intentional differences and unresolved keys.
    assert run.issue_count() == 0
    assert all(record['sha256_verified'] for record in run.meta['files'])
    assert run.settings.decimal_scale == 24
    run.con.execute('CREATE TEMP TABLE expected_groups (label VARCHAR, status VARCHAR, legacy_count BIGINT, saas_count BIGINT)')
    run.con.execute("INSERT INTO expected_groups SELECT printf('CDR_%07d',i), ?,1,1 FROM range(?) t(i)", [SAME, common])
    run.con.executemany('INSERT INTO expected_groups VALUES (?,?,?,?)', expected)
    # The fixture has two distinct IMSIs sharing one charging ID. All other
    # controlled key fields are equal after normalization, except missing IMSI.
    run.con.execute('''CREATE TEMP VIEW actual_groups AS
        SELECT *, CASE WHEN profile<>'GPRS' THEN 'UNSUPPORTED'
            WHEN json_extract_string(substr(match_key,6),'$[6]')='SAME_CHARGING_ID'
                THEN CASE json_extract_string(substr(match_key,6),'$[0]')
                    WHEN '000000000000001' THEN 'KEY_IMSI_A'
                    WHEN '000000000000002' THEN 'KEY_IMSI_B' END
            ELSE json_extract_string(substr(match_key,6),'$[6]') END AS label
        FROM cdr_matches''')
    assert run.con.execute('''SELECT count(*) FROM actual_groups a FULL OUTER JOIN expected_groups e USING(label)
        WHERE a.label IS NULL OR e.label IS NULL OR a.status IS DISTINCT FROM e.status
           OR a.legacy_count IS DISTINCT FROM e.legacy_count OR a.saas_count IS DISTINCT FROM e.saas_count''').fetchone()[0] == 0
    assert run.con.execute('SELECT count(*),count(DISTINCT label) FROM actual_groups').fetchone() == (common + len(expected),) * 2
    assert not run.con.execute('''SELECT 1 FROM cdr_matches m JOIN legacy l ON m.legacy_id=l.record_id
        JOIN saas s ON m.saas_id=s.record_id WHERE l.GPRS_CHARGING_ID IS DISTINCT FROM s.GPRS_CHARGING_ID
        OR l.IMSI IS DISTINCT FROM s.IMSI LIMIT 1''').fetchall()

    counts = {}
    for index, source in enumerate(('legacy', 'saas'), start=2):
        count = common + sum(case[index] for case in expected)
        counts[source] = count
        for table in (source, f'raw_{source}', f'keys_{source}'):
            assert run.con.execute(f'SELECT count(*) FROM {table}').fetchone()[0] == count
        assert run.con.execute(f'SELECT count(DISTINCT record_id) FROM {source}').fetchone()[0] == count
        assert run.con.execute(f'''SELECT count(*) FROM {source} n LEFT JOIN raw_{source} r ON n.record_id=r.ingest_id
            WHERE r.ingest_id IS NULL OR n.source_file IS DISTINCT FROM r.source_file
               OR n.source_sheet IS DISTINCT FROM r.source_sheet OR n.source_row IS DISTINCT FROM r.source_row''').fetchone()[0] == 0
        # Every record is represented exactly once: a unique-key pointer, or an
        # ambiguous member. This also covers orphans and identical duplicates.
        run.con.execute(f'''CREATE TEMP VIEW covered_{source} AS
            SELECT {source}_id AS record_id FROM cdr_matches WHERE {source}_id IS NOT NULL
            UNION ALL SELECT record_id FROM ambiguous_members WHERE source='{source}' ''')
        assert run.con.execute(f'SELECT count(*),count(DISTINCT record_id) FROM covered_{source}').fetchone() == (count, count)
        assert run.con.execute(f'SELECT count(*) FROM {source} ANTI JOIN covered_{source} USING(record_id)').fetchone()[0] == 0
    assert run.con.execute('SELECT sum(legacy_count),sum(saas_count) FROM matching_summary').fetchone() == (counts['legacy'], counts['saas'])
    ambiguous = sum(left + right for _, status, left, right in expected if status == AMBIGUOUS)
    assert run.con.execute('SELECT count(*) FROM ambiguous_records').fetchone()[0] == ambiguous == 19
    assert not run.con.execute("SELECT 1 FROM cdr_matches WHERE status=? AND (legacy_id IS NOT NULL OR saas_id IS NOT NULL)", [AMBIGUOUS]).fetchall()
    assert not run.con.execute('''SELECT 1 FROM field_differences f JOIN cdr_matches m USING(match_key)
        WHERE m.status=?''', [AMBIGUOUS]).fetchall()
    for label, field, difference in (('RATING_CHANGE', 'RATED_AMOUNT_TAP', EPSILON), ('RATE_CHANGE', 'RATE', EPSILON)):
        assert run.con.execute('''SELECT difference FROM field_differences JOIN actual_groups USING(match_key)
            WHERE label=? AND field=? AND difference_type='NORMALIZED_VALUE' ''', [label, field]).fetchall() == [(difference,)]
    with localcontext() as context:
        context.prec = 80
        assert run.con.execute('SELECT CURRENCY,legacy_amount,saas_amount,amount_variance FROM revenue ORDER BY CURRENCY').fetchall() == [
            ('IDR', Decimal(0), Decimal(2), Decimal(2)),
            ('USD', (common + 5) * Decimal(AMOUNT) + 49, (common + 5) * Decimal(AMOUNT) + 54 + EPSILON, 5 + EPSILON),
        ]
        attribution = run.con.execute('''SELECT CURRENCY,matched_rating_differences,saas_only_amount,
            negative_legacy_only_amount,unresolved_ambiguous_difference,total_variance,explained_variance,residual,status
            FROM revenue_attribution ORDER BY CURRENCY''').fetchall()
        assert attribution == [('IDR', Decimal(2), Decimal(0), Decimal(0), Decimal(0), Decimal(2), Decimal(2), Decimal(0), 'MATCH'),
                               ('USD', -2 + EPSILON, Decimal(7), Decimal(-4), Decimal(4), 5 + EPSILON, 5 + EPSILON, Decimal(0), 'MATCH')]
    return counts


def _snapshot(run, *, details=False):
    tables = ('counts', 'revenue', 'hpmn_set', 'vpmn_set', 'hpmn', 'vpmn', 'pairs', 'matching_summary', 'revenue_attribution')
    result = {table: run.con.execute(f'SELECT * FROM {table} ORDER BY ALL').fetchall() for table in tables}
    if details:
        result['matches'] = run.con.execute('SELECT match_key,profile,status,legacy_count,saas_count FROM cdr_matches ORDER BY ALL').fetchall()
        result['differences'] = run.con.execute('''SELECT match_key,profile,HPMN,VPMN,CURRENCY,field,
            legacy_raw,saas_raw,legacy_normalized,saas_normalized,difference,difference_type
            FROM field_differences ORDER BY ALL''').fetchall()
        columns = ','.join('"' + field + '"' for field in FIELDS)
        result['ambiguous'] = run.con.execute(f'SELECT match_key,profile,source,reason,{columns} FROM ambiguous_records ORDER BY ALL').fetchall()
    return result


def test_shuffled_multifile_matching_is_invariant_to_chunk_size(tmp_path):
    common = 20_017
    baseline = None
    for name, seed, chunk, split in (('ordered', None, 10_000, False), ('shuffled', 49, 10_000, False),
                                     ('different_chunk', 49, 250_000, False), ('partitioned', 777, 20_000, True)):
        paths, expected = _inputs(tmp_path / name, common, seed=seed, split=split)
        directory = execute(paths['legacy'], paths['saas'], root=tmp_path / 'runs', full_audit=True,
                            settings=Settings(memory_limit='1GB', threads=2, decimal_scale=24,
                                              normalization_chunk_rows=chunk, max_elapsed_seconds=300))
        with Run(directory) as run:
            _assert_global_oracle(run, common, expected)
            actual = _snapshot(run, details=True)
            if baseline is None:
                baseline = actual
            else:
                assert actual == baseline, name


def test_global_matching_across_thirty_populated_chunks_and_default_chunk_size(tmp_path):
    common = 300_000
    paths, expected = _inputs(tmp_path / 'input', common, seed=191, distant=True)
    baseline = None
    evidence = []
    for chunk in (10_000, 250_000):
        directory = execute(paths['legacy'], paths['saas'], root=tmp_path / 'runs',
                            settings=Settings(memory_limit='1GB', threads=2, decimal_scale=24,
                                              normalization_chunk_rows=chunk, max_elapsed_seconds=300))
        with Run(directory) as run:
            counts = _assert_global_oracle(run, common, expected)
            anchor = run.con.execute("SELECT legacy_id,saas_id,status FROM actual_groups WHERE label='CDR_0000000'").fetchone()
            assert anchor == (1, 300_000, SAME)
            assert tuple((record - 1) // chunk + 1 for record in anchor[:2]) == ((1, 30) if chunk == 10_000 else (1, 2))
            for source, label in (('legacy', 'DUP_LEFT'), ('saas', 'DUP_RIGHT')):
                pointers = run.con.execute(f'SELECT min(record_id),max(record_id) FROM {source} WHERE GPRS_CHARGING_ID=?', [label]).fetchone()
                assert (pointers[0] - 1) // chunk != (pointers[1] - 1) // chunk
            actual = _snapshot(run)
            if baseline is None:
                baseline = actual
            else:
                assert actual == baseline
            evidence.append({'run_directory': str(directory), 'chunk': chunk, 'source_rows': counts,
                             'anchor_ids': anchor[:2], 'anchor_chunks': [(pointer - 1) // chunk + 1 for pointer in anchor[:2]],
                             'result': run.meta['result'], 'processing_seconds': run.meta['processing_seconds'],
                             'verified': 'All global groups, row coverage, duplicate membership, exact totals and attribution matched their independent oracles.'})
    (tmp_path / 'global_matching_evidence.json').write_text(json.dumps(evidence, indent=2), encoding='utf-8')


def test_default_250k_chunks_match_sparse_record_ids_in_chunk_1_and_30(tmp_path, monkeypatch):
    # Sparse IDs exercise the real default chunk boundaries with cheap empty
    # ranges. The populated 30-chunk test above separately checks dense inputs.
    normalize = ingestion.normalize

    def place_in_distant_chunk(run, source, options):
        if source == 'saas':
            run.con.execute("UPDATE raw_saas SET ingest_id=7250001 WHERE GPRS_CHARGING_ID='ANCHOR'")
        normalize(run, source, options)

    monkeypatch.setattr(ingestion, 'normalize', place_in_distant_chunk)
    left = write_csv(tmp_path / 'legacy.csv', [cdr(GPRS_CHARGING_ID='ANCHOR'), cdr(GPRS_CHARGING_ID='LEFT')])
    right = write_csv(tmp_path / 'saas.csv', [cdr(GPRS_CHARGING_ID='RIGHT'), cdr(GPRS_CHARGING_ID='ANCHOR')])
    directory = execute([left], [right], root=tmp_path / 'runs',
                        settings=Settings(memory_limit='256MB', threads=2, decimal_scale=24))
    with Run(directory) as run:
        assert run.meta['status'] == 'COMPLETED', run.meta
        assert run.settings.normalization_chunk_rows == 250_000
        assert run.con.execute('SELECT legacy_id,saas_id,status FROM cdr_matches WHERE legacy_id=1').fetchone() == (1, 7_250_001, SAME)
        assert run.con.execute('SELECT status,count(*) FROM cdr_matches GROUP BY status ORDER BY status').fetchall() == [
            ('LEGACY_ONLY', 1), (SAME, 1), ('SAAS_ONLY', 1)]
        assert run.con.execute('SELECT count(*) FROM legacy').fetchone()[0] == 2
        assert run.con.execute('SELECT count(*) FROM saas').fetchone()[0] == 2
        assert all(row['residual'] == 0 for row in run.rows('SELECT residual FROM revenue_attribution'))
