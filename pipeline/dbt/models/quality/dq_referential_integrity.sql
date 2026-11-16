-- Verwijzingen naar rijen die niet in dezelfde batch zitten. Lege verwijzingen vallen onder null_spike.
with rules as (
  select 'order_items' as table_name, 'order_id' as column_name, 'orders' as related_table, i.order_id as k,
         o.order_id is null as bad
  from {{ source('landing', 'order_items') }} i
  left join (select distinct order_id from {{ source('landing', 'orders') }}) o using (order_id)
  where i.order_id is not null
  union all
  select 'order_payments', 'order_id', 'orders', p.order_id, o.order_id is null
  from {{ source('landing', 'order_payments') }} p
  left join (select distinct order_id from {{ source('landing', 'orders') }}) o using (order_id)
  where p.order_id is not null
  union all
  select 'orders', 'customer_id', 'customers', o.customer_id, c.customer_id is null
  from {{ source('landing', 'orders') }} o
  left join (select distinct customer_id from {{ source('landing', 'customers') }}) c using (customer_id)
  where o.customer_id is not null
  union all
  select 'order_items', 'product_id', 'products', i.product_id, p.product_id is null
  from {{ source('landing', 'order_items') }} i
  left join (select distinct product_id from {{ source('landing', 'products') }}) p using (product_id)
  where i.product_id is not null
)
select 'referential_integrity' as issue_type, table_name, column_name,
       'fk_' || table_name || '_' || related_table as check_name,
       count(*) filter (where bad) as n_affected, count(*) as n_total,
       to_json(list(k) filter (where bad)[1:5])::varchar as sample,
       'verwijst naar een rij die niet bestaat in ' || related_table as details, related_table
from rules
group by table_name, column_name, related_table
having count(*) filter (where bad) > 0
