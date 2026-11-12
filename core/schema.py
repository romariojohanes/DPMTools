"""Confirmed query schema and explicit normalization policy."""
import re
from dataclasses import dataclass
from pathlib import Path

FIELDS = (
    'EVENT_DATE EVENT_BEGIN_TIME EVENT_END_TIME TYPE SERVICE SERVICE_TYPE IMSI VPMN HPMN '
    'VOLUME_INCOMING VOLUME_OUTGOING VOLUME_TOTAL VOLUME_CHARGED_UNITS DURATION_TOTAL '
    'CURRENCY RATED_AMOUNT_TAP TAX_AMOUNT_TAP FINAL_TAX_AMOUNT RATE RATE_UNIT '
    'ROUNDED_UNIT_ID GPRS_CHARGING_ID ANUM BNUM'
).split()
NUMERIC = 'VOLUME_INCOMING VOLUME_OUTGOING VOLUME_TOTAL VOLUME_CHARGED_UNITS DURATION_TOTAL RATED_AMOUNT_TAP TAX_AMOUNT_TAP FINAL_TAX_AMOUNT RATE'.split()
IDENTIFIERS = ['IMSI', 'ANUM', 'BNUM', 'GPRS_CHARGING_ID']
REQUIRED_VALUES = ['EVENT_DATE', 'CURRENCY', 'HPMN', 'VPMN', 'RATED_AMOUNT_TAP']
MATCH_PROFILES = {
    'GPRS': ['IMSI', 'EVENT_DATE', 'EVENT_BEGIN_TIME', 'EVENT_END_TIME', 'VPMN', 'HPMN', 'GPRS_CHARGING_ID'],
    'VOICE': ['IMSI', 'EVENT_DATE', 'EVENT_BEGIN_TIME', 'EVENT_END_TIME', 'VPMN', 'HPMN', 'ANUM', 'BNUM'],
    'SMS': ['IMSI', 'EVENT_DATE', 'EVENT_BEGIN_TIME', 'VPMN', 'HPMN', 'ANUM', 'BNUM'],
    'VOICE-MO': ['IMSI', 'EVENT_DATE', 'EVENT_BEGIN_TIME', 'EVENT_END_TIME', 'VPMN', 'HPMN', 'ANUM'],
    'VOICE-MT': ['IMSI', 'EVENT_DATE', 'EVENT_BEGIN_TIME', 'EVENT_END_TIME', 'VPMN', 'HPMN', 'BNUM'],
    'SMS-MO': ['IMSI', 'EVENT_DATE', 'EVENT_BEGIN_TIME', 'VPMN', 'HPMN', 'ANUM'],
    'SMS-MT': ['IMSI', 'EVENT_DATE', 'EVENT_BEGIN_TIME', 'VPMN', 'HPMN', 'BNUM'],
}
COMPARISON_FIELDS = ['RATED_AMOUNT_TAP', 'CURRENCY', 'RATE', 'VOLUME_INCOMING', 'VOLUME_OUTGOING',
                     'VOLUME_TOTAL', 'VOLUME_CHARGED_UNITS', 'DURATION_TOTAL', 'TAX_AMOUNT_TAP', 'FINAL_TAX_AMOUNT']
NULLS = ['', 'NULL', 'NONE', 'N/A', 'NA', '\\N']


def canonical(value: str) -> str:
    return re.sub(r'[^A-Z0-9]+', '_', str(value).strip().lstrip('\ufeff').upper()).strip('_')


def quote(value: str) -> str:
    """SQL literal; never use identifiers supplied by users in SQL."""
    return "'" + str(value).replace("'", "''") + "'"


@dataclass(frozen=True)
class InputOptions:
    delimiter: str = ','
    encoding: str = 'utf-8-sig'
    decimal_separator: str = '.'
    date_format: str = '%m/%d/%Y'
    sheet_name: str | None = None
    column_mapping: dict[str, str] | None = None

    def __post_init__(self):
        if len(self.delimiter) != 1 or self.delimiter in '\r\n"':
            raise ValueError('CSV delimiter must be one character, excluding quotes/newlines.')
        if self.decimal_separator not in ('.', ','):
            raise ValueError('Decimal separator must be a dot or comma.')
        if self.date_format not in ('%Y-%m-%d', '%Y%m%d', '%d/%m/%Y', '%m/%d/%Y'):
            raise ValueError('Select an explicit supported date format.')
        ''.encode(self.encoding)  # fail early for unknown codecs
        if self.column_mapping is not None:
            if not isinstance(self.column_mapping, dict):
                raise ValueError('Column mapping must be a source-column to canonical-field object.')
            normalized = {}
            for source, target in self.column_mapping.items():
                source_name, target_name = canonical(source), canonical(target)
                if not source_name or target_name not in FIELDS:
                    raise ValueError('Column mappings require nonblank source columns and known canonical fields.')
                if source_name in normalized:
                    raise ValueError(f'Duplicate normalized mapping source: {source_name}.')
                normalized[source_name] = target_name
            if len(set(normalized.values())) != len(normalized):
                raise ValueError('Each canonical field can be assigned by at most one mapped source column.')
            object.__setattr__(self, 'column_mapping', normalized)


@dataclass(frozen=True)
class Settings:
    memory_limit: str = '4GB'
    temp_directory: str | None = None
    decimal_scale: int = 12
    threads: int = 4
    max_temp_directory_size: str = '20GB'
    free_space_reserve_gb: int = 50
    process_memory_limit_gb: int = 14
    available_memory_floor_gb: int = 4
    monitor_interval_seconds: float = 2.0
    normalization_chunk_rows: int = 250_000
    max_elapsed_seconds: float | None = None

    def __post_init__(self):
        if not 0 <= self.decimal_scale <= 30:
            raise ValueError('Decimal scale must be between 0 and 30 (38 total digits).')
        if not re.fullmatch(r'[1-9][0-9]*(?:\.[0-9]+)?\s*(?:MB|GB)', self.memory_limit, re.I):
            raise ValueError('Memory limit must be a positive MB or GB value.')
        if not 1 <= self.threads <= 32:
            raise ValueError('Threads must be between 1 and 32.')
        if not re.fullmatch(r'[1-9][0-9]*(?:\.[0-9]+)?\s*(?:MB|GB)', self.max_temp_directory_size, re.I):
            raise ValueError('Maximum temporary storage must be a positive MB or GB value.')
        if self.free_space_reserve_gb < 1:
            raise ValueError('Free-space reserve must be at least 1 GB.')
        if self.process_memory_limit_gb < 1 or self.available_memory_floor_gb < 1:
            raise ValueError('Process and available-memory limits must be positive.')
        if not 0.5 <= self.monitor_interval_seconds <= 60:
            raise ValueError('Monitor interval must be between 0.5 and 60 seconds.')
        if not 10_000 <= self.normalization_chunk_rows <= 2_000_000:
            raise ValueError('Normalization chunk size must be between 10,000 and 2,000,000 rows.')
        if self.max_elapsed_seconds is not None and self.max_elapsed_seconds <= 0:
            raise ValueError('Maximum elapsed time must be positive when configured.')


def resolve_inputs(entries: list[str | Path]) -> list[Path]:
    """Resolve individual paths or immediate directory contents, reject duplicate input."""
    result = []
    for entry in entries:
        path = Path(str(entry).strip().strip('"')).expanduser().resolve()
        if not path.exists():
            raise ValueError(f'Input does not exist: {path}')
        candidates = sorted(p for p in path.iterdir() if p.is_file() and p.suffix.lower() in ('.csv', '.xlsx') and not p.name.startswith('~$')) if path.is_dir() else [path]
        if not candidates:
            raise ValueError(f'No CSV/XLSX files in directory: {path}')
        for candidate in candidates:
            if candidate.suffix.lower() not in ('.csv', '.xlsx'):
                raise ValueError(f'Unsupported input: {candidate}')
            if candidate in result:
                raise ValueError(f'Duplicate input file: {candidate}')
            result.append(candidate)
    if not result:
        raise ValueError('Select at least one input file for each source.')
    return result
