import os
from pathlib import Path
from uuid import uuid4

import pytest

from core.engine import Run
from core.schema import Settings
from core.workflow import execute
from tools.generate_samples import cdr, write_csv, write_xlsx


@pytest.fixture
def tmp_path(tmp_path_factory, request):
    """Allow managed Windows environments to supply a non-ACL-rewritten test root."""
    configured = os.environ.get('DPM_TEST_TEMP_ROOT')
    if not configured:
        return tmp_path_factory.mktemp(request.node.name)
    root = Path(configured).resolve()
    root.mkdir(parents=True, exist_ok=True)
    target = root / f'{request.node.name}_{uuid4().hex[:8]}'
    target.mkdir()
    return target


@pytest.fixture
def run_pair(tmp_path):
    opened = []

    def create(legacy=None, saas=None, *, xlsx=False, **kwargs):
        left = write_csv(tmp_path / f'legacy_{len(opened)}.csv', legacy if legacy is not None else [cdr()])
        writer = write_xlsx if xlsx else write_csv
        right = writer(tmp_path / f'saas_{len(opened)}.{"xlsx" if xlsx else "csv"}', saas if saas is not None else [cdr()])
        path = execute([left], [right], settings=kwargs.pop('settings', Settings(memory_limit='256MB', threads=2)), root=tmp_path / 'runs', **kwargs)
        run = Run(path)
        opened.append(run)
        return run

    yield create
    for run in opened:
        run.close()
