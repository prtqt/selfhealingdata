-- Kolommen die ontbreken of onverwacht zijn, en waarden die niet in het afgesproken type passen.
{% set contract = var('contract') %}
with missing as (
  select 'schema_drift' as issue_type, c.table_name, c.column_name, 'missing_column' as check_name,
         f.row_count as n_affected, f.row_count as n_total, null::varchar as sample,
         'kolom uit het contract ontbreekt in de aanlevering' as details, null::varchar as related_table
  from {{ source('landing', 'contract') }} c
  join {{ source('landing', 'files') }} f on f.table_name = c.table_name and f.present
  where not exists (select 1 from {{ source('landing', 'columns_actual') }} a
                    where a.table_name = c.table_name and a.column_name = c.column_name)
),
unexpected as (
  select 'schema_drift', a.table_name, a.column_name, 'unexpected_column', f.row_count, f.row_count,
         null, 'kolom staat niet in het contract', null
  from {{ source('landing', 'columns_actual') }} a
  join {{ source('landing', 'files') }} f on f.table_name = a.table_name
  where not exists (select 1 from {{ source('landing', 'contract') }} c
                    where c.table_name = a.table_name and c.column_name = a.column_name)
),
types as (
  {{ no_findings() }}
  {% for table, spec in contract.items() %}{% for col, c in spec['columns'].items() if c['type'] != 'varchar' %}
  union all
  select 'schema_drift', '{{ table }}', '{{ col }}', 'type_mismatch',
         count(*) filter (where "{{ col }}" is not null and try_cast("{{ col }}" as {{ c['type'] }}) is null),
         count(*) filter (where "{{ col }}" is not null),
         to_json(list("{{ col }}") filter (where "{{ col }}" is not null and try_cast("{{ col }}" as {{ c['type'] }}) is null)[1:5])::varchar,
         'waarde is niet te lezen als {{ c['type'] }}', null
  from {{ source('landing', table) }}
  {% endfor %}{% endfor %}
)
select * from missing
union all select * from unexpected
union all select * from types where n_total > 0 and n_affected::double / n_total > {{ var('type_threshold') }}
