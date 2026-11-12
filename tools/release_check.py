"""DPM+ managed-folder release preflight; performs no installation or mutation."""
from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
IMPORT_NAMES = {
    'streamlit': 'streamlit',
    'duckdb': 'duckdb',
    'openpyxl': 'openpyxl',
    'pytest': 'pytest',
}


def pinned(path):
    result = {}
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip().lstrip('\ufeff')
        if not line or line.startswith('#'):
            continue
        match = re.fullmatch(r'([A-Za-z0-9_.-]+)==([^\s]+)', line)
        if not match:
            raise ValueError(f'{path.name}: dependency is not exactly pinned: {line}')
        result[match.group(1).lower()] = match.group(2)
    return result


def verify():
    if sys.version_info[:2] != (3, 12):
        raise RuntimeError(f'Python 3.12 is required; found {sys.version.split()[0]}.')
    direct = pinned(ROOT / 'requirements.txt')
    locked = pinned(ROOT / 'requirements-lock.txt')
    errors = []
    for distribution, expected in direct.items():
        if locked.get(distribution) != expected:
            errors.append(f'{distribution}: requirements.txt={expected}, lock={locked.get(distribution)}')
            continue
        try:
            actual = importlib.metadata.version(distribution)
            importlib.import_module(IMPORT_NAMES.get(distribution, distribution.replace('-', '_')))
        except (importlib.metadata.PackageNotFoundError, ImportError) as exc:
            errors.append(f'{distribution}: missing ({exc})')
        else:
            if actual != expected:
                errors.append(f'{distribution}: expected {expected}, installed {actual}')
    if errors:
        raise RuntimeError('Dependency preflight failed:\n  - ' + '\n  - '.join(errors))
    return direct


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--preflight', action='store_true', help='Verify the managed-folder runtime.')
    parser.parse_args()
    versions = verify()
    print('DPM+ v1.0 preflight passed: Python ' + sys.version.split()[0] + '; ' +
          ', '.join(f'{name} {version}' for name, version in versions.items()))


if __name__ == '__main__':
    main()
