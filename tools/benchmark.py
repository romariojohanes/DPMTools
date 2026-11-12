"""Repeatable synthetic benchmark; intentionally opt-in, never uses subscriber data."""
import argparse
import json
import platform
import ctypes
from time import perf_counter
from pathlib import Path

import duckdb

from core.engine import Run
from core.schema import Settings
from core.workflow import execute
from core.reporting import export_detail, export_report
from tools.generate_samples import cdr, write_csv, write_xlsx


def peak_memory_bytes():
    if platform.system() == 'Windows':
        from ctypes import wintypes
        class Counters(ctypes.Structure):
            _fields_ = [('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD)] + [
                (name, ctypes.c_size_t) for name in ('PeakWorkingSetSize', 'WorkingSetSize', 'QuotaPeakPagedPoolUsage',
                'QuotaPagedPoolUsage', 'QuotaPeakNonPagedPoolUsage', 'QuotaNonPagedPoolUsage', 'PagefileUsage', 'PeakPagefileUsage')]
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        psapi = ctypes.WinDLL('psapi', use_last_error=True)
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        if psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            return counters.PeakWorkingSetSize
        return None
    import resource
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak if platform.system() == 'Darwin' else peak * 1024


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rows', type=int, default=100_000, help='Rows per source')
    parser.add_argument('--directory', default='data/benchmark')
    parser.add_argument('--memory', default='4GB')
    parser.add_argument('--format', choices=['csv', 'xlsx'], default='csv')
    args = parser.parse_args()
    if args.rows < 1:
        parser.error('--rows must be positive')
    root = Path(args.directory).resolve()
    paths = []
    for source in ('legacy', 'saas'):
        writer = write_csv if args.format == 'csv' else write_xlsx
        paths.append(writer(root / f'{source}.{args.format}', (cdr(HPMN=f'TST{i % 50:02d}', VPMN=f'TST{(i * 7) % 70:02d}', GPRS_CHARGING_ID=str(i), RATED_AMOUNT_TAP='0.123456789012') for i in range(args.rows))))
    started = perf_counter()
    directory = execute([paths[0]], [paths[1]], root=root / 'runs', settings=Settings(memory_limit=args.memory), traffic_date='2026-10-25', progress=print)
    with Run(directory) as run:
        reporting_error = None
        if run.meta['status'] == 'COMPLETED':
            try:
                export_report(run, 'Executive Summary')
                export_detail(run, 'cdr_details', 'parquet')
            except Exception as exc:
                reporting_error = str(exc)
        elapsed = perf_counter() - started
        result = {'rows_per_source': args.rows, 'total_rows': args.rows * 2, 'elapsed_seconds': round(elapsed, 3),
                  'rows_per_second': round(args.rows * 2 / elapsed), 'status': run.meta['status'], 'result': run.meta.get('result'),
                  'run_directory': str(directory), 'memory_limit': args.memory, 'threads': run.settings.threads,
                  'python': platform.python_version(), 'duckdb': duckdb.__version__, 'platform': platform.platform(),
                  'input_format': args.format, 'stage_seconds': run.meta.get('stage_seconds', {}),
                  'processing_seconds': run.meta['processing_seconds'], 'reporting_seconds': run.meta.get('reporting_seconds'),
                  'peak_process_memory_bytes': peak_memory_bytes(), 'error': run.meta.get('error'),
                  'reporting_error': reporting_error, 'benchmark_status': 'FAILED' if reporting_error or run.meta['status'] != 'COMPLETED' else 'COMPLETED',
                  'scope': 'Synthetic ingestion, exact normalization, aggregation, matching, field comparison, attribution, Parquet detail and bounded Excel summary. Generation excluded from elapsed time, included in process memory. No production-scale claim.'}
    output = root / 'benchmark_result.json'
    output.write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result, indent=2))
    if result['benchmark_status'] != 'COMPLETED' or result['result'] != 'MATCH':
        raise SystemExit('Benchmark reconciliation failed; inspect the run validation report.')


if __name__ == '__main__':
    main()
