select purchased_at::date as order_date, count(*) as n_orders,
       round(sum(items_value), 2) as items_value, round(sum(paid_value), 2) as paid_value
from {{ ref('fct_orders') }}
group by 1
