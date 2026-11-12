"""Disk-backed composite-key matching. Duplicate groups are never paired."""
from core.schema import FIELDS, NUMERIC, COMPARISON_FIELDS, MATCH_PROFILES, quote


def validate_profiles(profiles):
    if not isinstance(profiles, dict) or not profiles:
        raise ValueError('Matching profiles must be a nonempty service-type mapping.')
    for service, keys in profiles.items():
        if not isinstance(service, str) or not service or service != service.strip().upper():
            raise ValueError('Matching profile names must be uppercase SERVICE_TYPE values.')
        if not isinstance(keys, list) or not keys or len(set(keys)) != len(keys):
            raise ValueError('Each matching profile needs distinct key fields.')
        if any(key not in FIELDS or key in NUMERIC or key == 'CURRENCY' or key in ('RATE_UNIT', 'ROUNDED_UNIT_ID') for key in keys):
            raise ValueError('Matching keys must use known non-rating fields.')
    return profiles


def match_cdrs(run, profiles=None, full_audit=False):
    if run.issue_count():
        raise ValueError('Validation errors must be resolved before CDR matching.')
    profiles = validate_profiles(profiles if profiles is not None else MATCH_PROFILES)
    run.meta.update(matching_profiles=profiles, full_field_audit=full_audit, phase=2)
    key_cases, complete_cases = [], []
    for service, fields in profiles.items():
        values = ','.join(f'CAST("{f}" AS VARCHAR)' for f in fields)
        key_cases.append(f'WHEN SERVICE_TYPE={quote(service)} THEN CAST(to_json([{values}]) AS VARCHAR)')
        complete_cases.append(f'WHEN SERVICE_TYPE={quote(service)} THEN ' + ' AND '.join(f'"{f}" IS NOT NULL' for f in fields))
    # JSON arrays provide collision-free serialization; source row positions never enter the key.
    key = "coalesce(SERVICE_TYPE, '<NULL>') || ':' || CASE " + ' '.join(key_cases) + " ELSE 'UNSUPPORTED' END"
    complete = 'CASE ' + ' '.join(complete_cases) + ' ELSE false END'
    for source in ('legacy', 'saas'):
        run.con.execute(f'''CREATE TABLE keys_{source} AS SELECT record_id,
            {key} AS match_key, SERVICE_TYPE AS profile, {complete} AS key_complete FROM {source}''')
    run.con.execute('''CREATE TABLE key_groups AS
        WITH records AS (
            SELECT *, 'legacy' AS source FROM keys_legacy UNION ALL SELECT *, 'saas' AS source FROM keys_saas)
        SELECT match_key, any_value(profile) AS profile, bool_and(key_complete) AS key_complete,
            count(*) FILTER(WHERE source='legacy') AS legacy_count,
            count(*) FILTER(WHERE source='saas') AS saas_count,
            min(record_id) FILTER(WHERE source='legacy') AS legacy_pointer,
            min(record_id) FILTER(WHERE source='saas') AS saas_pointer
        FROM records GROUP BY match_key''')
    run.con.execute('''CREATE TABLE cdr_matches AS
        WITH checked AS (SELECT *, key_complete AND legacy_count<=1 AND saas_count<=1 AS unique_key FROM key_groups),
        groups AS (SELECT *, CASE WHEN unique_key THEN legacy_pointer END AS legacy_id,
                             CASE WHEN unique_key THEN saas_pointer END AS saas_id FROM checked)
        SELECT g.match_key, g.profile, g.legacy_count, g.saas_count,
            CASE WHEN g.unique_key THEN g.legacy_pointer END AS legacy_id,
            CASE WHEN g.unique_key THEN g.saas_pointer END AS saas_id,
            CASE WHEN NOT g.unique_key THEN 'DUPLICATE_OR_AMBIGUOUS'
                 WHEN g.legacy_count=0 THEN 'SAAS_ONLY' WHEN g.saas_count=0 THEN 'LEGACY_ONLY'
                 WHEN l.RATED_AMOUNT_TAP=s.RATED_AMOUNT_TAP AND l.CURRENCY=s.CURRENCY THEN 'MATCHED_SAME_RATING'
                 ELSE 'MATCHED_DIFFERENT_RATING' END AS status
        FROM groups g LEFT JOIN legacy l ON g.legacy_id=l.record_id
                      LEFT JOIN saas s ON g.saas_id=s.record_id''')
    run.con.execute('''CREATE TABLE matching_summary AS SELECT status, profile,
        count(*) AS key_groups, sum(legacy_count) AS legacy_count, sum(saas_count) AS saas_count,
        sum(saas_count)-sum(legacy_count) AS count_variance FROM cdr_matches GROUP BY status,profile''')
    run.con.execute('''CREATE VIEW cdr_details AS SELECT m.*,
        coalesce(l.HPMN,s.HPMN) AS HPMN, coalesce(l.VPMN,s.VPMN) AS VPMN,
        coalesce(l.CURRENCY,s.CURRENCY) AS CURRENCY, l.CURRENCY AS legacy_currency, s.CURRENCY AS saas_currency,
        l.source_file AS legacy_file, l.source_sheet AS legacy_sheet, l.source_row AS legacy_row,
        s.source_file AS saas_file, s.source_sheet AS saas_sheet, s.source_row AS saas_row,
        l.RATED_AMOUNT_TAP AS legacy_amount, s.RATED_AMOUNT_TAP AS saas_amount,
        CASE WHEN l.CURRENCY=s.CURRENCY THEN s.RATED_AMOUNT_TAP-l.RATED_AMOUNT_TAP END AS rating_difference
        FROM cdr_matches m LEFT JOIN legacy l ON m.legacy_id=l.record_id LEFT JOIN saas s ON m.saas_id=s.record_id''')
    for name, status in (('rating_differences', 'MATCHED_DIFFERENT_RATING'), ('legacy_only', 'LEGACY_ONLY'), ('saas_only', 'SAAS_ONLY')):
        run.con.execute(f'CREATE VIEW {name} AS SELECT * FROM cdr_details WHERE status={quote(status)}')
    # Materialize only ambiguous member identities before touching the wide source
    # rows.  Each member is keyed by its source record_id, so duplicate groups are
    # never paired across sources and cannot form a Cartesian product.
    members = []
    for source in ('legacy', 'saas'):
        members.append(f'''SELECT m.match_key,m.profile,m.status,m.legacy_count,m.saas_count,
            {quote(source)} AS source, CASE WHEN NOT g.key_complete THEN 'INCOMPLETE_OR_UNSUPPORTED_KEY'
                ELSE 'NON_UNIQUE_KEY' END AS reason, k.record_id
            FROM keys_{source} k JOIN cdr_matches m USING(match_key) JOIN key_groups g USING(match_key)
            WHERE m.status='DUPLICATE_OR_AMBIGUOUS' ''')
    run.con.execute('CREATE TABLE ambiguous_members AS ' + ' UNION ALL '.join(members))
    branches = []
    for source in ('legacy', 'saas'):
        branches.append(f'''SELECT a.* EXCLUDE(record_id),r.* FROM ambiguous_members a
            JOIN {source} r ON r.record_id=a.record_id WHERE a.source={quote(source)}''')
    run.con.execute('CREATE VIEW ambiguous_records AS ' + ' UNION ALL '.join(branches))
    run.con.execute('''CREATE VIEW duplicate_keys AS SELECT g.* EXCLUDE(legacy_pointer,saas_pointer),m.status
        FROM key_groups g JOIN cdr_matches m USING(match_key) WHERE m.status='DUPLICATE_OR_AMBIGUOUS' ''')


def compare_fields(run, full_audit=False):
    fields = FIELDS if full_audit else COMPARISON_FIELDS
    # Process one field at a time: avoid materializing a 24x expansion for all matched CDRs.
    run.con.execute('''CREATE TABLE field_differences (
        match_key VARCHAR, profile VARCHAR, HPMN VARCHAR, VPMN VARCHAR, CURRENCY VARCHAR,
        legacy_file VARCHAR, legacy_sheet VARCHAR, legacy_row BIGINT,
        saas_file VARCHAR, saas_sheet VARCHAR, saas_row BIGINT,
        field VARCHAR, legacy_raw VARCHAR, saas_raw VARCHAR,
        legacy_normalized VARCHAR, saas_normalized VARCHAR, difference DECIMAL(38,''' + str(run.settings.decimal_scale) + '''),
        difference_type VARCHAR)''')
    comparisons = []
    for field in fields:
        mismatch = f'l."{field}" IS DISTINCT FROM s."{field}"'
        raw_mismatch = f'lr."{field}" IS DISTINCT FROM sr."{field}"'
        difference = f's."{field}"-l."{field}"' if field in NUMERIC else 'NULL'
        if field in ('RATED_AMOUNT_TAP', 'RATE', 'TAX_AMOUNT_TAP', 'FINAL_TAX_AMOUNT'):
            difference = f'CASE WHEN l.CURRENCY=s.CURRENCY THEN {difference} END'
        comparisons.append(f"""CASE WHEN ({mismatch}) OR ({raw_mismatch}) THEN struct_pack(
            field := {quote(field)}, legacy_raw := lr."{field}", saas_raw := sr."{field}",
            legacy_normalized := CAST(l."{field}" AS VARCHAR), saas_normalized := CAST(s."{field}" AS VARCHAR),
            difference := CAST({difference} AS DECIMAL(38,{run.settings.decimal_scale})),
            difference_type := CASE WHEN {mismatch} THEN 'NORMALIZED_VALUE' ELSE 'RAW_FORMAT_ONLY' END) END""")
    # Join each source once; filter out equal fields before expanding the mismatch list.
    run.con.execute(f'''INSERT INTO field_differences
        SELECT match_key,profile,HPMN,VPMN,CURRENCY,legacy_file,legacy_sheet,legacy_row,saas_file,saas_sheet,saas_row,
            d.field,d.legacy_raw,d.saas_raw,d.legacy_normalized,d.saas_normalized,d.difference,d.difference_type
        FROM (SELECT m.match_key,m.profile,l.HPMN,l.VPMN,l.CURRENCY,
            l.source_file AS legacy_file,l.source_sheet AS legacy_sheet,l.source_row AS legacy_row,
            s.source_file AS saas_file,s.source_sheet AS saas_sheet,s.source_row AS saas_row,
            unnest(list_filter([{','.join(comparisons)}], x -> x IS NOT NULL)) AS d
            FROM cdr_matches m JOIN legacy l ON m.legacy_id=l.record_id JOIN saas s ON m.saas_id=s.record_id
            JOIN raw_legacy lr ON (l.source_file,l.source_sheet,l.source_row)=(lr.source_file,lr.source_sheet,lr.source_row)
            JOIN raw_saas sr ON (s.source_file,s.source_sheet,s.source_row)=(sr.source_file,sr.source_sheet,sr.source_row))''')


def attribute_revenue(run):
    parts = []
    for source, sign in (('legacy', '-'), ('saas', '+')):
        parts.append(f'''SELECT r.CURRENCY,m.status,{sign}r.RATED_AMOUNT_TAP AS contribution
            FROM {source} r JOIN keys_{source} k USING(record_id) JOIN cdr_matches m USING(match_key)''')
    run.con.execute('''CREATE TABLE revenue_attribution AS WITH contributions AS (''' + ' UNION ALL '.join(parts) + ''')
        SELECT CURRENCY,
            sum(CASE WHEN status='MATCHED_DIFFERENT_RATING' THEN contribution ELSE 0 END) AS matched_rating_differences,
            sum(CASE WHEN status='SAAS_ONLY' THEN contribution ELSE 0 END) AS saas_only_amount,
            sum(CASE WHEN status='LEGACY_ONLY' THEN contribution ELSE 0 END) AS negative_legacy_only_amount,
            sum(CASE WHEN status='DUPLICATE_OR_AMBIGUOUS' THEN contribution ELSE 0 END) AS unresolved_ambiguous_difference
        FROM contributions GROUP BY CURRENCY''')
    run.con.execute('''ALTER TABLE revenue_attribution ADD COLUMN total_variance DECIMAL(38,''' + str(run.settings.decimal_scale) + ''')''')
    run.con.execute('''UPDATE revenue_attribution a SET total_variance=r.amount_variance FROM revenue r WHERE a.CURRENCY=r.CURRENCY''')
    run.con.execute('''CREATE OR REPLACE TABLE revenue_attribution AS SELECT *,
        matched_rating_differences+saas_only_amount+negative_legacy_only_amount+unresolved_ambiguous_difference AS explained_variance,
        total_variance-(matched_rating_differences+saas_only_amount+negative_legacy_only_amount+unresolved_ambiguous_difference) AS residual,
        CASE WHEN total_variance=matched_rating_differences+saas_only_amount+negative_legacy_only_amount+unresolved_ambiguous_difference
            THEN 'MATCH' ELSE 'RECONCILIATION_FAILED' END AS status FROM revenue_attribution''')
    if run.con.execute("SELECT count(*) FROM revenue_attribution WHERE status<>'MATCH'").fetchone()[0]:
        raise ValueError('Revenue attribution has an unexplained residual.')
