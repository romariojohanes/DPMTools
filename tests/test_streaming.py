from decimal import Decimal
from pathlib import Path
import tracemalloc
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from openpyxl.utils import get_column_letter

from core.engine import Run
from core.schema import FIELDS, NUMERIC, InputOptions, Settings
from core.workflow import execute
from core.xlsx_stream import DiskStrings
from tools.generate_samples import cdr, write_csv, write_xlsx


def test_native_excel_xml_digits_preserved_exactly(tmp_path):
    exact = '0.1234567890123456789012'
    left = write_csv(tmp_path / 'left.csv', [cdr(RATED_AMOUNT_TAP=exact)])
    row = cdr()
    for field in NUMERIC:
        row[field] = float(row[field])
    original = write_xlsx(tmp_path / 'original.xlsx', [row])
    right = tmp_path / 'right.xlsx'
    ns = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
    with ZipFile(original) as source, ZipFile(right, 'w') as target:
        for info in source.infolist():
            data = source.read(info.filename)
            if info.filename == 'xl/worksheets/sheet1.xml':
                root = ET.fromstring(data)
                cell = root.find(f".//{ns}c[@r='{get_column_letter(FIELDS.index('RATED_AMOUNT_TAP')+1)}2']")
                cell.find(ns + 'v').text = exact
                data = ET.tostring(root)
            target.writestr(info, data)
    path = execute([left], [right], root=tmp_path / 'runs', saas_options=InputOptions(decimal_separator=','),
                   settings=Settings(memory_limit='256MB', decimal_scale=24))
    with Run(path) as run:
        assert run.meta['status'] == 'COMPLETED', run.meta
        assert run.meta['result'] == 'MATCH'
        assert run.rows('SELECT RATED_AMOUNT_TAP FROM saas')[0]['RATED_AMOUNT_TAP'] == Decimal(exact)
        assert run.rows('SELECT RATED_AMOUNT_TAP FROM raw_saas')[0]['RATED_AMOUNT_TAP'] == exact


def test_shared_strings_workbook_uses_disk_dictionary(tmp_path):
    original = write_xlsx(tmp_path / 'original.xlsx', [cdr(IMSI='000012345')])
    right = tmp_path / 'shared.xlsx'
    ns = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
    strings = ET.Element(ns + 'sst')
    index = 0
    with ZipFile(original) as source, ZipFile(right, 'w') as target:
        for info in source.infolist():
            data = source.read(info.filename)
            if info.filename == 'xl/worksheets/sheet1.xml':
                root = ET.fromstring(data)
                for cell in root.iter(ns + 'c'):
                    if cell.get('t') == 'inlineStr':
                        inline = cell.find(ns + 'is')
                        strings.append(ET.fromstring(ET.tostring(inline).replace(b':is', b':si')))
                        cell.remove(inline)
                        cell.set('t', 's')
                        ET.SubElement(cell, ns + 'v').text = str(index)
                        index += 1
                data = ET.tostring(root)
            elif info.filename == '[Content_Types].xml':
                root = ET.fromstring(data)
                ET.SubElement(root, '{http://schemas.openxmlformats.org/package/2006/content-types}Override',
                              PartName='/xl/sharedStrings.xml', ContentType='application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml')
                data = ET.tostring(root)
            target.writestr(info, data)
        target.writestr('xl/sharedStrings.xml', ET.tostring(strings))
    left = write_csv(tmp_path / 'left.csv', [cdr(IMSI='000012345')])
    with Run(execute([left], [right], root=tmp_path / 'runs')) as run:
        assert run.meta['status'] == 'COMPLETED', run.meta
        assert run.meta['result'] == 'MATCH'
        assert not (run.directory / 'shared_strings.sqlite').exists()


def test_shared_string_cache_has_fixed_bound(tmp_path):
    strings = DiskStrings(tmp_path / 'strings.sqlite')
    try:
        strings.connection.executemany('INSERT INTO strings VALUES (?,?)', ((i, str(i)) for i in range(5000)))
        for i in range(5000):
            assert strings[i] == str(i)
        assert len(strings.cache) == 4096
    finally:
        strings.close()


def test_large_generator_pipeline_has_bounded_python_memory(tmp_path):
    tracemalloc.start()
    try:
        paths = [write_csv(tmp_path / f'{side}.csv', (cdr(GPRS_CHARGING_ID=str(i)) for i in range(20000))) for side in ('left', 'right')]
        with Run(execute([paths[0]], [paths[1]], root=tmp_path / 'runs',
                         settings=Settings(memory_limit='256MB', threads=2, normalization_chunk_rows=10_000))) as run:
            assert run.meta['status'] == 'COMPLETED', run.meta
            assert run.con.execute('SELECT count(*) FROM cdr_matches').fetchone()[0] == 20000
        _, peak = tracemalloc.get_traced_memory()
        assert peak < 64 * 1024 * 1024  # Python only; native process peak is measured by tools.benchmark.
    finally:
        tracemalloc.stop()


def test_recognized_time_formats_match(run_pair):
    run = run_pair(legacy=[cdr(EVENT_BEGIN_TIME='080000', EVENT_END_TIME='80030.0')],
                   saas=[cdr(EVENT_BEGIN_TIME='8:00:00', EVENT_END_TIME='08:00:30')])
    assert run.meta['result'] == 'MATCH', run.meta
    assert run.rows('SELECT EVENT_BEGIN_TIME FROM legacy')[0]['EVENT_BEGIN_TIME'] == '08:00:00'


def test_consolidated_normalization_preserves_independent_transforms(run_pair):
    legacy = cdr(EVENT_BEGIN_TIME='080000', EVENT_END_TIME='80030.0',
                 IMSI='1.2345E+4', RATED_AMOUNT_TAP='1.25e1')
    saas = cdr(EVENT_BEGIN_TIME='08:00:00', EVENT_END_TIME='08:00:30',
               IMSI='12345', RATED_AMOUNT_TAP='12.5')
    run = run_pair(legacy=[legacy], saas=[saas])

    assert run.meta['status'] == 'COMPLETED', run.meta
    assert run.meta['result'] == 'MATCH'
    normalized = run.rows('''SELECT EVENT_BEGIN_TIME, EVENT_END_TIME, IMSI, RATED_AMOUNT_TAP
                             FROM legacy''')[0]
    assert normalized == {
        'EVENT_BEGIN_TIME': '08:00:00',
        'EVENT_END_TIME': '08:00:30',
        'IMSI': '12345',
        'RATED_AMOUNT_TAP': Decimal('12.500000000000'),
    }



def test_stale_xlsx_dimensions_do_not_hide_records(tmp_path):
    rows = [cdr(GPRS_CHARGING_ID=str(i)) for i in range(3)]
    original = write_xlsx(tmp_path / 'original.xlsx', rows)
    right = tmp_path / 'stale.xlsx'
    ns = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
    with ZipFile(original) as source, ZipFile(right, 'w') as target:
        for info in source.infolist():
            data = source.read(info.filename)
            if info.filename == 'xl/worksheets/sheet1.xml':
                root = ET.fromstring(data)
                root.insert(0, ET.Element(ns + 'dimension', ref='A1:A1'))
                data = ET.tostring(root)
            target.writestr(info, data)
    left = write_csv(tmp_path / 'left.csv', rows)
    with Run(execute([left], [right], root=tmp_path / 'runs')) as run:
        assert run.meta['result'] == 'MATCH', run.meta
        assert run.con.execute('SELECT count(*) FROM saas').fetchone()[0] == 3
