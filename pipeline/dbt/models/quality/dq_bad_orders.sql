-- Bestellingen die door een rijgebonden bevinding zijn geraakt. De pipeline houdt alleen deze vast
-- en laat de rest van de batch alvast door, mits dat deel zelf de poort haalt.
{% set contract = var('contract') %}
with bad as (
  select null::varchar as order_id where false
  {% for table in ['orders', 'order_items', 'order_payments'] if table in contract %}
  -- lege verplichte waarden
  union all
  select order_id from {{ source('landing', table) }}
  where {% for col, c in contract[table]['columns'].items() if c.get('required') %}"{{ col }}" is null{% if not loop.last %} or {% endif %}{% endfor %}
  -- dubbele sleutels
  union all
  select order_id from {{ source('landing', table) }}
  group by {{ contract[table]['key'] | map('tojson') | join(', ') }} having count(*) > 1
  {% endfor %}
  union all select k from {{ ref('dq_invalid_rows') }} where bad
  union all
  select o.order_id from {{ source('landing', 'orders') }} o
  left join (select distinct customer_id from {{ source('landing', 'customers') }}) c using (customer_id)
  where o.customer_id is not null and c.customer_id is null
  union all
  select i.order_id from {{ source('landing', 'order_items') }} i
  left join (select distinct product_id from {{ source('landing', 'products') }}) p using (product_id)
  where i.product_id is not null and p.product_id is null
)
select distinct order_id from bad where order_id is not null
