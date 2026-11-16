-- Sleutels die vaker dan één keer voorkomen.
{% set contract = var('contract') %}
with checks as (
  {{ no_findings() }}
  {% for table, spec in contract.items() %}
  {% set key = spec['key'] | map('tojson') | join(', ') %}
  union all
  select 'duplicates', '{{ table }}', '{{ spec['key'] | join('+') }}', 'unique_key',
         coalesce(sum(cnt - 1), 0)::bigint, (select count(*) from {{ source('landing', table) }}),
         to_json(list(concat_ws('|', {{ key }}))[1:5])::varchar,
         'sleutel komt meer dan één keer voor', null
  from (
    select {{ key }}, count(*) as cnt from {{ source('landing', table) }}
    where {% for k in spec['key'] %}"{{ k }}" is not null{% if not loop.last %} and {% endif %}{% endfor %}
    group by all having count(*) > 1
  )
  {% endfor %}
)
select * from checks where n_affected > 0
