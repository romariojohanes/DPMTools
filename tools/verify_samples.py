"""Create a real synthetic sample run and all reports for local review."""
import json
from pathlib import Path

from core.engine import Run
from core.reporting import REPORTS, export_report
from core.workflow import execute


def main():
    root = Path(__file__).resolve().parents[1]
    directory = execute([root / 'samples' / 'matching' / 'legacy.csv'],
                        [root / 'samples' / 'matching' / 'saas.xlsx'],
                        root=root / 'data' / 'runs', traffic_date='2026-10-25', progress=print)
    with Run(directory) as run:
        if run.meta['status'] != 'COMPLETED' or run.meta.get('result') != 'MATCH':
            raise SystemExit(f'Sample verification failed: {run.meta}')
        reports = [str(export_report(run, name)) for name in REPORTS]
        result = {'run_id': run.meta['run_id'], 'status': run.meta['status'], 'result': run.meta['result'],
                  'run_directory': str(directory), 'reports': reports, 'synthetic_data_only': True}
    output = root / 'data' / 'sample_verification.json'
    output.write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
