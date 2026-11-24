"""Opt-in synthetic ingestion/normalization profiling in a fresh worker process.

Generate once with --generate --rows N, then run each implementation in a separate
process using --label before/after. No production inputs or existing runs are used.
"""
import argparse
import hashlib
import importlib.util
import json
from decimal import Decimal, localcontext
from pathlib import Path
from time import perf_counter

import duckdb

from core.engine import Run
from core.monitoring import ResourceMonitor
from core.schema import InputOptions, Settings
from tools.generate_samples import cdr, write_csv


class ProfileConnection:
    """Read DuckDB counters on the query's own worker, never on the monitor thread."""

    def __init__(self, connection):
        self.connection = connection
        self.queries = []
        self.memory = []

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def execute(self, sql, params=None):
        started = perf_counter()
        result = self.connection.execute(sql, params or [])
        elapsed = perf_counter() - started
        self.queries.append({'sql': sql, 'seconds': elapsed})
        # Only inspect after statements without a result to consume. The scalar
        # INSERT result and SELECT cursors must remain available to their caller.
        if sql.startswith(('CREATE ', 'UPDATE ', 'DROP ', 'ALTER ')):
            rows = self.connection.execute('SELECT * FROM duckdb_memory()').fetchall()
            self.memory.append({'query': len(self.queries) - 1,
                                'tags': {tag: {'memory': memory, 'spill': spill}
                                         for tag, memory, spill in rows if memory or spill}})
        return result


def generate(directory, rows):
    path = directory / 'synthetic.csv'
    if path.exists():
        raise ValueError('Refusing to overwrite an existing fixture.')
    write_csv(path, (cdr(IMSI=f'TEST_IMSI_{i:015}', ANUM=f'TEST_A_{i:015}',
                         BNUM=f'TEST_B_{i:015}', GPRS_CHARGING_ID=str(i),
                         EVENT_BEGIN_TIME='080000', EVENT_END_TIME='080030',
                         RATED_AMOUNT_TAP='0.123456789012345678901234',
                         RATE='0.000000000000000000000001') for i in range(rows)))
    with path.open('rb') as handle:
        digest = hashlib.file_digest(handle, 'sha256').hexdigest()
    (directory / 'fixture.json').write_text(json.dumps(
        {'rows': rows, 'bytes': path.stat().st_size, 'sha256': digest}), encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', required=True)
    parser.add_argument('--generate', action='store_true')
    parser.add_argument('--rows', type=int, default=500_000)
    parser.add_argument('--label', default='current')
    parser.add_argument('--module', help='Snapshot of core/ingestion.py for a before comparison')
    parser.add_argument('--memory', default='4GB')
    parser.add_argument('--threads', type=int, default=4)
    parser.add_argument('--chunk', type=int, default=250_000)
    parser.add_argument('--raw-only', action='store_true', help='Measure CSV ingestion without normalization')
    args = parser.parse_args()
    directory = Path(args.directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    if args.generate:
        if args.rows < 1:
            parser.error('--rows must be positive')
        generate(directory, args.rows)
        return
    target = directory / (args.label + '.json')
    if target.exists():
        raise ValueError('Refusing to overwrite benchmark evidence.')
    fixture = json.loads((directory / 'fixture.json').read_text(encoding='utf-8'))
    module_path = Path(__file__).resolve().parents[1] / 'core' / 'ingestion.py'
    if args.module:
        module_path = Path(args.module).resolve()
    spec = importlib.util.spec_from_file_location('benchmark_ingestion', module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if args.raw_only:
        module.normalize = lambda *unused: None  # Benchmark scope only; production code is untouched.
    implementation = module.ingest
    settings = Settings(memory_limit=args.memory, threads=args.threads, decimal_scale=24,
                        normalization_chunk_rows=args.chunk, max_elapsed_seconds=600,
                        monitor_interval_seconds=0.5)
    run = Run.create(directory / 'runs', settings)
    profile = ProfileConnection(run.con)
    run.con = profile
    monitor = ResourceMonitor(run)
    started = perf_counter()
    result = {'label': args.label, 'fixture': fixture, 'settings': vars(settings),
              'duckdb': duckdb.__version__, 'run_directory': str(run.directory),
              'implementation_sha256': hashlib.sha256(module_path.read_bytes()).hexdigest(),
              'scope': ('One-source synthetic CSV ingestion only; normalization/reconciliation excluded.' if args.raw_only else
                        'One-source synthetic ingestion and normalization; no production-scale certification.')}
    try:
        monitor.start()
        run.safety_check = monitor.raise_if_stopped
        source_path = Path(fixture.get('path', directory / 'synthetic.csv'))
        implementation(run, 'legacy', [source_path], InputOptions())
        run.emit('verification')
        monitor.checkpoint()
        table = 'raw_legacy' if args.raw_only else 'legacy'
        count, amount, rate = run.con.execute(
            f'SELECT count(*), sum(CAST(RATED_AMOUNT_TAP AS DECIMAL(38,24))), '
            f'sum(CAST(RATE AS DECIMAL(38,24))) FROM {table}').fetchone()
        with localcontext() as context:
            context.prec = 60
            assert count == fixture['rows']
            assert amount == count * Decimal(fixture.get('amount_per_row', '0.123456789012345678901234'))
            assert rate == count * Decimal(fixture.get('rate_per_row', '0.000000000000000000000001'))
        assert run.issue_count() == 0
        assert run.meta['files'][0]['sha256_verified']
        assert run.meta['files'][0]['sha256_before'] == fixture['sha256']
        assert run.meta['files'][0]['bytes'] == fixture['bytes']
        result.update(status='PASSED', rows=count, amount=str(amount), rate=str(rate))
    except Exception as exc:
        result.update(status='FAILED', error=str(exc), error_type=type(exc).__name__)
        raise
    finally:
        run.safety_check = None
        monitor.finish()
        run.emit('finished')
        result.update(seconds=perf_counter() - started, stage_seconds=run.meta.get('stage_seconds'),
                      resource_summary=run.meta['resource_summary'])
        result['stage_working_set_peak_bytes'] = {
            stage: max(s.get('working_set_bytes', 0) for s in monitor.samples if s['stage'] == stage)
            for stage in {s['stage'] for s in monitor.samples}}
        run.meta['status'] = 'BENCHMARK_' + result['status']
        run.save()
        run.close()
        (directory / (args.label + '_queries.json')).write_text(
            json.dumps({'queries': profile.queries, 'memory': profile.memory}, indent=2), encoding='utf-8')
        target.write_text(json.dumps(result, indent=2), encoding='utf-8')
        print(json.dumps(result), flush=True)


if __name__ == '__main__':
    main()
