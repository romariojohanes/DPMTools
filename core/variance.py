"""Backward-compatible, exact percentage variance report projections."""
from decimal import Decimal, localcontext, ROUND_HALF_EVEN
from uuid import uuid4


PERCENT_SCALE = 12


def _percentage_text(numerator, denominator, scale):
    if numerator is None or denominator is None:
        return None
    numerator, denominator = Decimal(numerator), Decimal(denominator)
    if denominator == 0:
        return None
    with localcontext() as context:
        # Retain every input digit, including scale-30 amounts and rounding ties.
        context.prec = 160 + scale
        result = numerator * 100 / abs(denominator)
        return format(result.quantize(Decimal(1).scaleb(-scale), rounding=ROUND_HALF_EVEN), 'f')


def register_percentage_function(connection):
    # Python functions belong to their registering connection. A private name
    # prevents another viewer/export connection from retaining a closed owner's
    # callback. Saved views reference the stable, connection-local macro instead.
    name = 'variance_percentage_' + uuid4().hex
    connection.create_function(name, _percentage_text,
                               ['VARCHAR', 'VARCHAR', 'INTEGER'], 'VARCHAR',
                               null_handling='special')
    connection.execute(f'''CREATE OR REPLACE TEMP MACRO exact_variance_percentage(n,d,s) AS
        {name}(CAST(n AS VARCHAR),CAST(d AS VARCHAR),CAST(s AS INTEGER))''')


def percentage_sql(numerator, denominator, scale=PERCENT_SCALE):
    """Return exact DECIMAL SQL for numerator / abs(denominator) * 100.

    Original DECIMAL inputs are passed as exact text to decimal arithmetic;
    the result retains ``scale`` fractional digits without FLOAT/DOUBLE division.
    """
    return (f'CAST(exact_variance_percentage(({numerator}),({denominator}),{scale}) '
            f'AS DECIMAL(38,{scale}))')


def enhanced_query(run, table):
    """Project variance fields for both current and historical completed runs."""
    columns = [row[0] for row in run.con.execute(f'DESCRIBE {table}').fetchall()]
    additions = []
    if table in ('revenue', 'hpmn', 'vpmn', 'pairs') and 'revenue_variance_pct' not in columns:
        additions.append(percentage_sql('amount_variance', 'legacy_amount') + ' AS revenue_variance_pct')
    if table in ('cdr_details', 'rating_differences'):
        if 'rating_variance_amount' not in columns:
            additions.append('rating_difference AS rating_variance_amount')
        if 'rating_variance_pct' not in columns:
            eligible = "status IN ('MATCHED_SAME_RATING','MATCHED_DIFFERENT_RATING')"
            if {'legacy_currency', 'saas_currency'} <= set(columns):
                eligible += ' AND legacy_currency=saas_currency'
            # Older reports may omit currency columns. Their saved difference
            # already encodes whether a comparable variance was available.
            additions.append('CASE WHEN ' + eligible + ' THEN ' +
                             percentage_sql('rating_difference', 'legacy_amount') +
                             ' END AS rating_variance_pct')
    if table == 'summary_comparison' and 'revenue_variance_pct' not in columns:
        difference = 'try_cast(total_charge_sdr_difference AS DECIMAL(38,12))'
        baseline = 'try_cast(legacy_total_charge_sdr AS DECIMAL(38,12))'
        additions.append(percentage_sql(difference, baseline) + ' AS revenue_variance_pct')
    return f"SELECT *{',' if additions else ''}{','.join(additions)} FROM {table}"
