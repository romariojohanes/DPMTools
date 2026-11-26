"""Backward-compatible, exact percentage variance report projections."""


PERCENT_SCALE = 12


def percentage_sql(numerator, denominator, scale=PERCENT_SCALE):
    """Return exact DECIMAL SQL for numerator / abs(denominator) * 100.

    Inputs and the persisted result are explicitly DECIMAL; the result retains
    ``scale`` fractional digits for export and is never stored as FLOAT/DOUBLE.
    """
    return (f"CASE WHEN ({denominator}) IS NULL OR ({denominator})=0 OR ({numerator}) IS NULL "
            f"THEN NULL ELSE CAST((CAST(({numerator}) AS DECIMAL(38,{scale})) / "
            f"abs(CAST(({denominator}) AS DECIMAL(38,{scale})))) * CAST(100 AS DECIMAL(38,0)) "
            f"AS DECIMAL(38,{scale})) END")


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
            eligible = ("status IN ('MATCHED_SAME_RATING','MATCHED_DIFFERENT_RATING') "
                        "AND legacy_currency=saas_currency")
            additions.append('CASE WHEN ' + eligible + ' THEN ' +
                             percentage_sql('rating_difference', 'legacy_amount') +
                             ' END AS rating_variance_pct')
    if table == 'summary_comparison' and 'revenue_variance_pct' not in columns:
        difference = 'try_cast(total_charge_sdr_difference AS DECIMAL(38,12))'
        baseline = 'try_cast(legacy_total_charge_sdr AS DECIMAL(38,12))'
        additions.append(percentage_sql(difference, baseline) + ' AS revenue_variance_pct')
    return f"SELECT *{',' if additions else ''}{','.join(additions)} FROM {table}"
