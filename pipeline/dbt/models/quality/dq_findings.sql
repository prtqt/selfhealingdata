-- Alle bevindingen van de zes controles in één formaat. De pipeline leest alleen deze tabel.
-- Ontbreekt een kolom, dan zijn de andere controles op die tabel ruis en vallen ze weg.
with all_findings as (
  select * from {{ ref('dq_schema_drift') }}
  union all select * from {{ ref('dq_null_spike') }}
  union all select * from {{ ref('dq_duplicates') }}
  union all select * from {{ ref('dq_invalid_values') }}
  union all select * from {{ ref('dq_referential_integrity') }}
  union all select * from {{ ref('dq_volume_anomaly') }}
),
broken_tables as (
  select distinct table_name from {{ ref('dq_schema_drift') }} where check_name = 'missing_column'
  union
  select table_name from {{ ref('dq_volume_anomaly') }} where check_name = 'missing_table'
)
select issue_type, table_name, column_name, check_name, n_affected, n_total,
       case when n_total > 0 then round(100.0 * n_affected / n_total, 2) end as pct_affected,
       coalesce(sample, '[]') as sample, details
from all_findings f
where issue_type in ('schema_drift', 'volume_anomaly')
   or (table_name not in (select table_name from broken_tables)
       and coalesce(related_table, '') not in (select table_name from broken_tables))
order by issue_type, table_name, column_name
