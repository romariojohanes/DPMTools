"""Isolated complete-export worker used by the Phase 3 benchmark supervisor."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

import duckdb

from core.engine import Run
from core.monitoring import ResourceMonitor
from core.reporting import DETAIL_TABLES, REPORTS, export_detail, export_report
from tools.phase3a_benchmark import _verify_xlsx


def _atomic_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, indent=2, default=str), encoding='utf-8')
    temporary.replace(path)


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        while block := handle.read(4 * 1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


class Journal:
    def __init__(self, staging, cancel_file):
        self.staging = Path(staging)
        self.cancel_file = Path(cancel_file)
        self.sequence = 0

    def __call__(self, event):
        if self.cancel_file.exists():
            raise RuntimeError('Export worker cancelled by supervisor.')
        self.sequence += 1
        record = {'sequence': self.sequence, 'pid': os.getpid(), **event}
        with (self.staging / 'export_progress.jsonl').open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(record, separators=(',', ':')) + '\n')
            handle.flush()
            os.fsync(handle.fileno())
        _atomic_json(self.staging / 'export_progress.json', record)


def run_worker(run_directory, staging, cancel_file):
    staging = Path(staging)
    staging.mkdir(parents=True, exist_ok=False)
    journal = Journal(staging, cancel_file)
    exports = []
    try:
        with Run(run_directory) as run:
            monitor = ResourceMonitor(run, append=True).start()
            original_journal = journal

            def checked_journal(event):
                monitor.checkpoint()
                original_journal(event)

            journal = checked_journal
            try:
                for report in REPORTS:
                    path = export_report(run, report, staging, checkpoint=journal)
                    journal({'event': 'artifact_verification_start', 'path': str(path)})
                    _verify_xlsx(path, run, report)
                    item = {'kind': 'excel', 'name': report, 'filename': path.name,
                            'bytes': path.stat().st_size, 'sha256': _sha256(path)}
                    exports.append(item)
                    journal({'event': 'artifact_verification_complete', **item})
                for table in sorted(DETAIL_TABLES):
                    path = export_detail(run, table, 'parquet', staging, checkpoint=journal)
                    journal({'event': 'artifact_verification_start', 'path': str(path)})
                    with duckdb.connect() as verifier:
                        count = verifier.execute(
                            'SELECT count(*) FROM read_parquet(?)', [str(path)]).fetchone()[0]
                    expected = run.con.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
                    if count != expected:
                        raise ValueError(f'{table} export: expected {expected}, found {count}')
                    item = {'kind': 'parquet', 'name': table, 'filename': path.name,
                            'bytes': path.stat().st_size, 'sha256': _sha256(path), 'rows': count}
                    exports.append(item)
                    journal({'event': 'artifact_verification_complete', **item})
            finally:
                monitor.finish()
        result = {'status': 'VERIFIED', 'exports': exports}
        _atomic_json(staging / 'worker_result.json', result)
        journal({'event': 'worker_complete', 'artifacts': len(exports)})
        return 0
    except BaseException as exc:
        _atomic_json(staging / 'incomplete_manifest.json', {
            'status': 'INCOMPLETE', 'error': str(exc), 'exports': exports})
        try:
            journal({'event': 'worker_failed', 'error': str(exc)})
        except BaseException:
            pass
        return 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--run-directory', required=True)
    parser.add_argument('--staging', required=True)
    parser.add_argument('--cancel-file', required=True)
    args = parser.parse_args()
    raise SystemExit(run_worker(args.run_directory, args.staging, args.cancel_file))


if __name__ == '__main__':
    main()
