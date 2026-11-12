"""Run storage and disk-backed query access. One isolated database per run."""
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
from time import perf_counter, sleep

import duckdb

from core.schema import Settings, quote


class Run:
    def __init__(self, directory: str | Path, settings: Settings | None = None, *, create=False):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.meta_path = self.directory / 'run.json'
        if create:
            self.meta = {'run_id': self.directory.name, 'created_at': datetime.now(timezone.utc).isoformat(),
                         'status': 'CREATED', 'settings': vars(settings or Settings()), 'files': []}
            self.save()
        else:
            self.meta = json.loads(self.meta_path.read_text(encoding='utf-8'))
        self.settings = Settings(**self.meta['settings'])
        self.progress = None
        self.started = perf_counter()
        self.stage_started = self.started
        self.current_stage = None
        self.con = duckdb.connect(str(self.directory / 'reconciliation.duckdb'))
        self.con.execute(f'SET memory_limit={quote(self.settings.memory_limit)}')
        temporary = Path(self.settings.temp_directory or self.directory / 'temp').resolve()
        temporary.mkdir(parents=True, exist_ok=True)
        self.temp_directory = temporary
        self.con.execute(f'SET temp_directory={quote(str(temporary))}')
        self.con.execute(f'SET max_temp_directory_size={quote(self.settings.max_temp_directory_size)}')
        # Investigation/export joins can otherwise allocate a hash buffer per worker.
        self.con.execute(f'SET threads={self.settings.threads if create else 1}')
        self.con.execute(f'SET preserve_insertion_order={"true" if create else "false"}')
        # Expand decimal scientific notation entirely as strings, without DOUBLE.
        for definition in (
            "num_m(v) AS split_part(lower(v),'e',1)",
            "num_e(v) AS CASE WHEN contains(lower(v),'e') THEN try_cast(split_part(lower(v),'e',2) AS INTEGER) ELSE 0 END",
            "num_d(v) AS replace(ltrim(num_m(v),'+-'),'.','')",
            "num_p(v) AS length(split_part(ltrim(num_m(v),'+-'),'.',1)) + num_e(v)",
            """exact_number(v) AS CASE WHEN regexp_full_match(v,'[+-]?[0-9]+([.][0-9]+)?([eE][+-]?[0-9]+)?')
                AND length(v) <= 256 AND num_p(v) BETWEEN -38 AND 76
                THEN CASE WHEN starts_with(v,'-') THEN '-' ELSE '' END ||
                    CASE WHEN num_p(v) <= 0 THEN '0.' || repeat('0', CAST(-num_p(v) AS INTEGER)) || num_d(v)
                    WHEN num_p(v) >= length(num_d(v)) THEN num_d(v) || repeat('0', CAST(num_p(v)-length(num_d(v)) AS INTEGER))
                    ELSE left(num_d(v),CAST(num_p(v) AS INTEGER)) || '.' || substr(num_d(v),CAST(num_p(v)+1 AS INTEGER)) END END""",
        ):
            self.con.execute('CREATE OR REPLACE TEMP MACRO ' + definition)
        if create:
            self.con.execute('''CREATE TABLE issues (
                source VARCHAR, source_file VARCHAR, source_sheet VARCHAR, source_row BIGINT,
                field VARCHAR, raw_value VARCHAR, code VARCHAR, message VARCHAR)''')

    @classmethod
    def create(cls, root='data/runs', settings=None):
        run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ_') + uuid4().hex[:8]
        return cls(Path(root) / run_id, settings, create=True)

    def save(self):
        temporary = self.meta_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.meta, indent=2, default=str), encoding='utf-8')
        # Windows can briefly deny replacement while the UI reads run metadata.
        for attempt in range(25):
            try:
                temporary.replace(self.meta_path)
                break
            except PermissionError:
                if attempt == 24:
                    raise
                sleep(0.01)

    def close(self):
        self.con.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def rows(self, sql, params=None):
        cursor = self.con.execute(sql, params or [])
        names = [item[0] for item in cursor.description]
        return [dict(zip(names, row)) for row in cursor.fetchall()]

    def issue_count(self):
        return self.con.execute('SELECT count(*) FROM issues').fetchone()[0]

    def emit(self, stage, **details):
        now = perf_counter()
        if stage != self.current_stage:
            if self.current_stage:
                durations = self.meta.setdefault('stage_seconds', {})
                durations[self.current_stage] = durations.get(self.current_stage, 0) + now - self.stage_started
            self.current_stage, self.stage_started = stage, now
        elapsed = now - self.stage_started
        event = dict(stage=stage, elapsed_seconds=round(now-self.started, 3), stage_elapsed_seconds=round(elapsed, 3), **details)
        if details.get('records') and elapsed:
            event['records_per_second'] = round(details['records'] / elapsed, 2)
        self.meta['progress'] = event
        self.save()
        if self.progress:
            self.progress(event)

    def table_page(self, table, *, filters=None, sort=None, descending=False, limit=100, offset=0):
        allowed = {'revenue', 'counts', 'hpmn_set', 'vpmn_set', 'hpmn', 'vpmn', 'pairs', 'issues', 'validation_files',
                   'matching_summary', 'cdr_details', 'rating_differences', 'legacy_only', 'saas_only',
                   'duplicate_keys', 'ambiguous_records', 'field_differences', 'revenue_attribution',
                   'summary_comparison', 'summary_legacy_only', 'summary_saas_only', 'summary_totals'}
        if table not in allowed:
            raise ValueError('Unknown report table.')
        columns = [r[0] for r in self.con.execute(f'DESCRIBE {table}').fetchall()]
        clauses, params = [], []
        for key, value in (filters or {}).items():
            if key not in columns:
                raise ValueError('Unknown filter column.')
            if value is None:
                clauses.append(f'"{key}" IS NULL')
            elif value != '':
                clauses.append(f'"{key}" = ?')
                params.append(value)
        where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
        # Stable tie-breaks prevent rows moving between pages when sort values repeat.
        tie_breaks = [c for c in ('PLMN', 'match_key', 'source', 'source_file', 'source_row', 'legacy_file', 'legacy_row', 'field',
                                  'HPMN', 'VPMN', 'CURRENCY', 'operator', 'metric', 'status', 'profile') if c in columns and c != sort]
        ordering = ([f'"{sort}" {"DESC" if descending else "ASC"}'] if sort in columns else []) + [f'"{c}"' for c in tie_breaks]
        order = ' ORDER BY ' + ','.join(ordering) if ordering else ''
        total = self.con.execute(f'SELECT count(*) FROM {table}{where}', params).fetchone()[0]
        return self.rows(f'SELECT * FROM {table}{where}{order} LIMIT ? OFFSET ?', params + [min(max(limit, 1), 500), max(offset, 0)]), total
