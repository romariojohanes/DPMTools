"""Orchestrate auditable, restart-independent reconciliation runs."""
from datetime import date, datetime, timezone
from time import perf_counter

from core.engine import Run
from core.ingestion import ingest
from core.reconciliation import reconcile
from core.matching import match_cdrs, compare_fields, attribute_revenue, validate_profiles
from core.monitoring import ResourceMonitor
from core.schema import InputOptions, quote, resolve_inputs


def validation_summary(run):
    run.con.execute('''CREATE OR REPLACE TABLE validation_files AS
        SELECT source, source_file, count(*) AS issue_count,
               count(DISTINCT (source_sheet,source_row)) FILTER (WHERE source_row IS NOT NULL) AS invalid_rows
        FROM issues GROUP BY source,source_file''')
    for record in run.meta['files']:
        found = run.con.execute('SELECT issue_count, invalid_rows FROM validation_files WHERE source=? AND source_file=?', [record['source'], record['path']]).fetchone()
        record['issue_count'], record['invalid_rows'] = found or (0, 0)
        if found:
            record['status'] = 'INVALID'
        elif record['status'] == 'READ':
            record['status'] = 'VALID'
    run.meta['issue_count'] = run.issue_count()


def execute(legacy_paths, saas_paths, *, legacy_options=None, saas_options=None,
            settings=None, root='data/runs', traffic_date=None, progress=None, matching_profiles=None, full_audit=False,
            cancel_event=None):
    if matching_profiles is not None:
        validate_profiles(matching_profiles)
    legacy, saas = resolve_inputs(legacy_paths), resolve_inputs(saas_paths)
    if set(legacy) & set(saas):
        raise ValueError('The same file cannot be used for both sources. Use independent source snapshots.')
    if traffic_date is not None:
        traffic_date = date.fromisoformat(str(traffic_date)).isoformat()
    run = Run.create(root, settings)
    run.progress = progress
    monitor = ResourceMonitor(run, cancel_event)
    started = perf_counter()
    run.meta.update(status='INGESTING', traffic_date=traffic_date,
                    options={'legacy': vars(legacy_options or InputOptions()), 'saas': vars(saas_options or InputOptions())})
    run.save()
    try:
        monitor.start()
        for source, paths, options in (('legacy', legacy, legacy_options or InputOptions()), ('saas', saas, saas_options or InputOptions())):
            ingest(run, source, paths, options)
            if traffic_date:
                run.con.execute(f'''INSERT INTO issues
                    SELECT {quote(source)}, source_file, source_sheet, source_row, 'EVENT_DATE',
                        CAST(EVENT_DATE AS VARCHAR), 'TRAFFIC_DATE_MISMATCH', 'Row falls outside selected traffic date.'
                    FROM {source} WHERE EVENT_DATE <> CAST(? AS DATE)''', [traffic_date])
        validation_summary(run)
        run.meta['date_ranges'] = {source: run.rows(f'SELECT min(EVENT_DATE) AS first_date, max(EVENT_DATE) AS last_date, count(DISTINCT EVENT_DATE) AS distinct_dates, count(*) AS row_count FROM {source}')[0] for source in ('legacy', 'saas')}
        if run.issue_count():
            run.meta['status'] = 'VALIDATION_FAILED'
        else:
            # Row pointers are persisted. Later set operations need no insertion-order buffer.
            run.con.execute('SET preserve_insertion_order=false')
            run.emit('aggregation')
            reconcile(run)
            run.meta['aggregate_result'] = run.meta['result']
            run.emit('matching')
            match_cdrs(run, matching_profiles, full_audit)
            run.emit('field_comparison')
            # Wide raw/normalized joins need one spill buffer, especially under small limits.
            run.con.execute('SET threads=1')
            compare_fields(run, full_audit)
            run.emit('revenue_attribution')
            attribute_revenue(run)
            if (run.con.execute("SELECT count(*) FROM cdr_matches WHERE status<>'MATCHED_SAME_RATING'").fetchone()[0]
                    or run.con.execute("SELECT count(*) FROM field_differences WHERE difference_type='NORMALIZED_VALUE'").fetchone()[0]):
                run.meta['result'] = 'MISMATCH'
            run.emit('parquet_export')
            for source in ('legacy', 'saas'):
                parquet = run.directory / f'{source}.parquet'
                run.con.execute(f'COPY {source} TO {quote(str(parquet))} (FORMAT PARQUET, COMPRESSION ZSTD)')
                # Keep one normalized copy, read through DuckDB directly from Parquet.
                run.con.execute(f'DROP TABLE {source}')
                run.con.execute(f'CREATE VIEW {source} AS SELECT * FROM read_parquet({quote(str(parquet))})')
            run.meta['status'] = 'COMPLETED'
    except Exception as exc:
        run.meta['status'] = ('CANCELLED' if monitor.stop_reason == 'USER_CANCELLED' else
                              'ABORTED_RESOURCE_LIMIT' if monitor.stop_reason else 'FAILED')
        run.meta.pop('result', None)
        run.meta['error'] = str(exc)
        run.con.execute('INSERT INTO issues VALUES (?,?,?,?,?,?,?,?)', ['run', '', '', None, '', None, 'RUN_ERROR', str(exc)])
        validation_summary(run)
    finally:
        monitor.finish()
        run.emit('finished')
        run.meta['completed_at'] = datetime.now(timezone.utc).isoformat()
        run.meta['processing_seconds'] = round(perf_counter() - started, 3)
        run.save()
        run.close()
    return run.directory
