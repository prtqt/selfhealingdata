-- Verplichte kolommen waarvan meer dan null_threshold van de rijen leeg is.
{% set contract = var('contract') %}
with checks as (
  {{ no_findings() }}
  {% for table, spec in contract.items() %}{% for col, c in spec['columns'].items() if c.get('required') %}
  union all
  select 'null_spike', '{{ table }}', '{{ col }}', 'required_not_null',
         count(*) filter (where "{{ col }}" is null), count(*),
         to_json(list(coalesce("{{ first_key(table) }}", '<leeg>')) filter (where "{{ col }}" is null)[1:5])::varchar,
         'verplichte kolom is leeg', null
  from {{ source('landing', table) }}
  {% endfor %}{% endfor %}
)
select * from checks
where n_total > 0 and n_affected::double / n_total > {{ var('null_threshold') }}
