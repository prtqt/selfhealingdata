{% macro generate_schema_name(custom_schema_name, node) -%}
  {{ custom_schema_name | trim if custom_schema_name else target.schema }}
{%- endmacro %}

{# Lege resultaatset in het vaste bevindingenformaat. #}
{% macro no_findings() -%}
select null::varchar as issue_type, null::varchar as table_name, null::varchar as column_name,
       null::varchar as check_name, 0::bigint as n_affected, 0::bigint as n_total,
       null::varchar as sample, null::varchar as details, null::varchar as related_table
where false
{%- endmacro %}

{# Eerste sleutelkolom van een tabel, om voorbeeldrijen aan te wijzen. #}
{% macro first_key(table) -%}
  {{ var('contract')[table]['key'][0] }}
{%- endmacro %}
