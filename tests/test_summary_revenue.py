import csv
from pathlib import Path

from openpyxl import load_workbook

from core.engine import Run
from core.schema import Settings
from core.summary_revenue import SummaryInputOptions, execute_summary, export_summary_xlsx


FIELDS = ['NO','PLMN','OPERATOR_NAME','FIRST_SEQ','LAST_SEQ','RECORDS','CHARGE_SDR','TAX_SDR','TOTAL_CHARGE_SDR','SUBSCRIBERS']


def record(plmn='51001', charge='10.10', **changes):
    value = {'NO':'1','PLMN':plmn,'OPERATOR_NAME':'Operator '+plmn,'FIRST_SEQ':'0001','LAST_SEQ':'0009',
             'RECORDS':'9','CHARGE_SDR':charge,'TAX_SDR':'1.01','TOTAL_CHARGE_SDR':'11.11','SUBSCRIBERS':'3'}
    value.update({key.upper(): str(item) for key, item in changes.items()})
    return value


def write(path, rows, fields=FIELDS, delimiter=','):
    with Path(path).open('w', encoding='utf-8', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, delimiter=delimiter, extrasaction='ignore')
        writer.writeheader(); writer.writerows(rows)
    return path


def run_summary(tmp_path, legacy, saas, **kwargs):
    left=write(tmp_path/'legacy.csv',legacy); right=write(tmp_path/'saas.csv',saas)
    directory=execute_summary([left],[right],root=tmp_path/'runs',settings=Settings(memory_limit='256MB',threads=1),**kwargs)
    return Run(directory)


def test_identical_reordered_and_exact_precision(tmp_path):
    rows=[record('51001', '0.123456789012345678901234567890', total_charge_sdr='1.133456789012345678901234567890'),
          record('51002','99.000000000000000000000000000001', total_charge_sdr='100.010000000000000000000000000001')]
    with run_summary(tmp_path,rows,list(reversed(rows))) as run:
        assert run.meta['result']=='MATCH'
        assert run.con.execute("SELECT count(*) FROM summary_comparison WHERE status='MATCH'").fetchone()[0]==2
        charge=run.con.execute("SELECT legacy_value FROM summary_totals WHERE metric='CHARGE_SDR'").fetchone()[0]
        assert charge=='99.123456789012345678901234567891'


def test_population_and_single_revenue_mismatch(tmp_path):
    with run_summary(tmp_path,[record('1'),record('2')],[record('2',charge='12',total_charge_sdr='13.01'),record('3')]) as run:
        assert dict(run.con.execute('SELECT PLMN,status FROM summary_comparison').fetchall())=={
            '1':'LEGACY_ONLY','2':'MISMATCH','3':'SAAS_ONLY'}
        assert run.meta['operator_counts']['legacy']==run.meta['operator_counts']['saas']==2


def test_different_operator_counts(tmp_path):
    with run_summary(tmp_path,[record('1')],[record('1'),record('2')]) as run:
        assert run.meta['operator_counts']['legacy']==1
        assert run.meta['operator_counts']['saas']==2


def test_comma_decimal_separator(tmp_path):
    comma = record(charge='1,25', tax_sdr='1,01', total_charge_sdr='2,26')
    left=write(tmp_path/'left.csv',[comma],delimiter=';')
    right=write(tmp_path/'right.csv',[comma],delimiter=';')
    options=SummaryInputOptions(delimiter=';',decimal_separator=',')
    path=execute_summary([left],[right],legacy_options=options,saas_options=options,root=tmp_path/'runs',
                         settings=Settings(memory_limit='256MB',threads=1))
    with Run(path) as run: assert run.meta['result']=='MATCH'


def test_duplicate_and_missing_plmn_are_explicit_validation_errors(tmp_path):
    with run_summary(tmp_path,[record('1'),record('1')],[record('')]) as run:
        assert run.meta['status']=='VALIDATION_FAILED'
        codes={row[0] for row in run.con.execute('SELECT code FROM issues').fetchall()}
        assert codes=={'DUPLICATE_KEY','MISSING_KEY'}


def test_independent_column_mappings(tmp_path):
    legacy_fields=['IDX','CODE','NAME','START','END','COUNT','CHARGE','TAX','TOTAL','SUBS']
    legacy_row=dict(zip(legacy_fields,record().values()))
    left=write(tmp_path/'legacy.csv',[legacy_row],legacy_fields)
    right=write(tmp_path/'saas.csv',[record()])
    mapping=dict(zip(legacy_fields,FIELDS))
    path=execute_summary([left],[right],legacy_options=SummaryInputOptions(column_mapping=mapping),
                         root=tmp_path/'runs',settings=Settings(memory_limit='256MB',threads=1))
    with Run(path) as run: assert run.meta['result']=='MATCH'


def test_complete_excel_export(tmp_path):
    with run_summary(tmp_path,[record('1'),record('2')],[record('2'),record('3')]) as run:
        path,rows,digest=export_summary_xlsx(run,tmp_path/'exports')
        assert rows==3 and len(digest)==64
        workbook=load_workbook(path,read_only=True,data_only=False)
        assert {'Executive Summary','PLMN Comparison','Legacy Only','SaaS Only','Integrity'} <= set(workbook.sheetnames)
        sheet=workbook['PLMN Comparison']
        assert sum(1 for _ in sheet.iter_rows())==4
        workbook.close()
        workbook=load_workbook(path,read_only=True,data_only=False)
        headers=[c.value for c in next(workbook['PLMN Comparison'].iter_rows())]
        assert {'legacy_charge_sdr','saas_charge_sdr','charge_sdr_difference','legacy_file','saas_file'} <= set(headers)
        workbook.close()


def test_subscribers_column_is_optional(tmp_path):
    fields = [field for field in FIELDS if field != 'SUBSCRIBERS']
    left = write(tmp_path/'legacy.csv', [record()], fields)
    right = write(tmp_path/'saas.csv', [record()], fields)
    path = execute_summary([left], [right], root=tmp_path/'runs', settings=Settings(memory_limit='256MB', threads=1))
    with Run(path) as run:
        row = run.rows('SELECT status,legacy_subscribers,saas_subscribers,subscribers_status FROM summary_comparison')[0]
        assert row == {'status':'MATCH', 'legacy_subscribers':'N/A', 'saas_subscribers':'N/A',
                       'subscribers_status':'NOT_COMPARED'}


def test_only_required_columns_are_accepted(tmp_path):
    fields = ['PLMN', 'TOTAL_CHARGE_SDR']
    left = write(tmp_path/'legacy.csv', [record()], fields)
    right = write(tmp_path/'saas.csv', [record()], fields)
    path = execute_summary([left], [right], root=tmp_path/'runs', settings=Settings(memory_limit='256MB', threads=1))
    with Run(path) as run:
        assert run.meta['result'] == 'MATCH'
        assert run.rows("SELECT charge_sdr_status,records_status FROM summary_comparison")[0] == {
            'charge_sdr_status':'NOT_COMPARED', 'records_status':'NOT_COMPARED'}


def test_different_optional_columns_are_preserved_and_not_compared(tmp_path):
    left = write(tmp_path/'legacy.csv', [record()], ['PLMN','TOTAL_CHARGE_SDR','CHARGE_SDR'])
    right = write(tmp_path/'saas.csv', [record()], ['PLMN','TOTAL_CHARGE_SDR','TAX_SDR'])
    path = execute_summary([left], [right], root=tmp_path/'runs', settings=Settings(memory_limit='256MB', threads=1))
    with Run(path) as run:
        row = run.rows('SELECT status,legacy_charge_sdr,saas_charge_sdr,charge_sdr_status,legacy_tax_sdr,saas_tax_sdr,tax_sdr_status FROM summary_comparison')[0]
        assert row['status'] == 'MATCH'
        assert (row['legacy_charge_sdr'], row['saas_charge_sdr'], row['charge_sdr_status']) == ('10.10','N/A','NOT_COMPARED')
        assert (row['legacy_tax_sdr'], row['saas_tax_sdr'], row['tax_sdr_status']) == ('N/A','1.01','NOT_COMPARED')


def test_missing_optional_value_is_distinct_from_zero(tmp_path):
    left_row, right_row = record(), record(subscribers='0')
    left_row['SUBSCRIBERS'] = ''
    with run_summary(tmp_path, [left_row], [right_row]) as run:
        row = run.rows('SELECT status,legacy_subscribers,saas_subscribers,subscribers_status FROM summary_comparison')[0]
        assert row == {'status':'MATCH', 'legacy_subscribers':'N/A', 'saas_subscribers':'0',
                       'subscribers_status':'NOT_COMPARED'}


def test_optional_financial_mismatch_is_reported(tmp_path):
    with run_summary(tmp_path, [record()], [record(charge_sdr='9.10')]) as run:
        row = run.rows('SELECT status,total_charge_sdr_status,charge_sdr_status FROM summary_comparison')[0]
        assert row == {'status':'MISMATCH', 'total_charge_sdr_status':'MATCH', 'charge_sdr_status':'MISMATCH'}
        assert run.meta['status'] == 'COMPLETED'
        assert run.meta['warning_count'] == 1


def test_excel_export_when_all_optional_columns_are_absent(tmp_path):
    fields = ['PLMN', 'TOTAL_CHARGE_SDR']
    left = write(tmp_path/'legacy.csv', [record()], fields)
    right = write(tmp_path/'saas.csv', [record()], fields)
    directory = execute_summary([left], [right], root=tmp_path/'runs', settings=Settings(memory_limit='256MB', threads=1))
    with Run(directory) as run:
        path, rows, _ = export_summary_xlsx(run, tmp_path/'exports')
    workbook = load_workbook(path, read_only=True, data_only=False)
    try:
        output_rows = workbook['PLMN Comparison'].iter_rows()
        headers = [cell.value for cell in next(output_rows)]
        values = dict(zip(headers, [cell.value for cell in next(output_rows)]))
        assert rows == 1
        assert values['legacy_charge_sdr'] == 'N/A'
        assert values['charge_sdr_status'] == 'NOT_COMPARED'
        assert 'Validation Warnings' in workbook.sheetnames
    finally:
        workbook.close()
