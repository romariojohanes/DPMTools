"""Bounded openpyxl reader, including XLSX shared-string dictionaries."""
from collections import OrderedDict
from contextlib import contextmanager
from pathlib import Path
import sqlite3
from xml.etree.ElementTree import iterparse

from openpyxl.cell.text import Text
from openpyxl.reader.excel import ExcelReader
from openpyxl.xml.constants import SHARED_STRINGS


class DiskStrings:
    def __init__(self, path):
        self.path = Path(path)
        self.connection = sqlite3.connect(self.path)
        self.connection.execute('PRAGMA cache_size=-4096')
        self.connection.execute('PRAGMA journal_mode=OFF')
        self.connection.execute('CREATE TABLE strings (id INTEGER PRIMARY KEY, value TEXT)')
        self.cache = OrderedDict()

    def __getitem__(self, index):
        if index in self.cache:
            self.cache.move_to_end(index)
            return self.cache[index]
        row = self.connection.execute('SELECT value FROM strings WHERE id=?', (index,)).fetchone()
        if row is None:
            raise IndexError('Invalid XLSX shared-string reference.')
        self.cache[index] = row[0]
        if len(self.cache) > 4096:
            self.cache.popitem(last=False)
        return row[0]

    def close(self):
        self.connection.close()
        self.cache.clear()
        self.path.unlink(missing_ok=True)


class StreamingExcelReader(ExcelReader):
    """Pinned openpyxl 3.1 reader with a disk-backed shared-string sequence."""
    def __init__(self, path, dictionary_path):
        super().__init__(path, read_only=True, data_only=False, keep_links=False)
        self.dictionary_path = dictionary_path
        self.dictionary = None

    def read_strings(self):
        content_type = self.package.find(SHARED_STRINGS)
        if content_type is None:
            return
        self.dictionary = DiskStrings(self.dictionary_path)
        tag = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}si'
        with self.archive.open(content_type.PartName.lstrip('/')) as source:
            parser = iterparse(source, events=('start', 'end'))
            _, root = next(parser)
            batch, index = [], 0
            for event, node in parser:
                if event == 'end' and node.tag == tag:
                    batch.append((index, Text.from_tree(node).content.replace('x005F_', '')))
                    index += 1
                    node.clear()
                    root.clear()
                    if len(batch) == 2000:
                        self.dictionary.connection.executemany('INSERT INTO strings VALUES (?,?)', batch)
                        batch.clear()
            if batch:
                self.dictionary.connection.executemany('INSERT INTO strings VALUES (?,?)', batch)
            self.dictionary.connection.commit()
        self.shared_strings = self.dictionary


@contextmanager
def streaming_workbook(path, directory):
    reader = StreamingExcelReader(path, Path(directory) / 'shared_strings.sqlite')
    try:
        reader.read()
        yield reader.wb
    finally:
        reader.archive.close()
        if reader.dictionary is not None:
            reader.dictionary.close()
