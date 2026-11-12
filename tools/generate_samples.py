"""Create synthetic CDRs only. Run: python -m tools.generate_samples."""
import csv
from pathlib import Path

from openpyxl import Workbook

from core.schema import FIELDS


def cdr(**overrides):
    values = dict.fromkeys(FIELDS, '')
    values.update(EVENT_DATE='10/25/2026', EVENT_BEGIN_TIME='080000_RAW', EVENT_END_TIME='080030_RAW',
                  TYPE='OUTBOUND', SERVICE='DATA', SERVICE_TYPE='GPRS', IMSI='TEST_IMSI_00001',
                  HPMN='TST01', VPMN='TST91', VOLUME_INCOMING='100', VOLUME_OUTGOING='200',
                  VOLUME_TOTAL='300', VOLUME_CHARGED_UNITS='1', DURATION_TOTAL='30',
                  CURRENCY='USD', RATED_AMOUNT_TAP='1.000000000001', TAX_AMOUNT_TAP='0',
                  FINAL_TAX_AMOUNT='0', RATE='0.0001', RATE_UNIT='MB', ROUNDED_UNIT_ID='1',
                  GPRS_CHARGING_ID='00000012345678901234', ANUM='TEST_A_0001', BNUM='TEST_B_0001')
    values.update(overrides)
    return values


def write_csv(path, rows, *, delimiter=',', encoding='utf-8-sig', fields=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding=encoding, newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=fields or FIELDS, delimiter=delimiter)
        writer.writeheader()
        writer.writerows(rows)
    return path


def write_xlsx(path, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook = Workbook(write_only=True)
    sheet = workbook.create_sheet('CDR')
    sheet.append(FIELDS)
    for row in rows:
        sheet.append([row.get(field) for field in FIELDS])
    workbook.save(path)
    return path


def main():
    root = Path(__file__).resolve().parents[1] / 'samples'
    rows = [cdr(), cdr(HPMN='TST02', VPMN='TST92', RATED_AMOUNT_TAP='2.999999999999'),
            cdr(HPMN='TST02', CURRENCY='IDR', RATED_AMOUNT_TAP='12500.25')]
    write_csv(root / 'matching' / 'legacy.csv', rows)
    write_xlsx(root / 'matching' / 'saas.xlsx', rows)
    write_csv(root / 'variance' / 'legacy.csv', rows)
    write_csv(root / 'variance' / 'saas.csv', [rows[0], cdr(HPMN='TST03', VPMN='TST93', RATED_AMOUNT_TAP='3.5'), rows[2]])
    write_csv(root / 'invalid' / 'legacy.csv', [cdr(RATED_AMOUNT_TAP='NOT_A_FINANCIAL_VALUE')])
    write_csv(root / 'invalid' / 'saas.csv', [cdr()])
    write_csv(root / 'comma_decimal' / 'legacy.csv', [cdr(RATED_AMOUNT_TAP='12,345678901234', RATE='0,0001')], delimiter=';')
    write_csv(root / 'comma_decimal' / 'saas.csv', [cdr(RATED_AMOUNT_TAP='12,345678901234', RATE='0,0001')], delimiter=';')
    for source in ('legacy', 'saas'):
        write_csv(root / 'multiple_files' / source / 'part_1.csv', rows[:2])
        write_xlsx(root / 'multiple_files' / source / 'part_2.xlsx', rows[2:])
    print(f'Synthetic samples generated in {root}')


if __name__ == '__main__':
    main()
