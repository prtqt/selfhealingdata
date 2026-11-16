with items as (
  select order_id, count(*) as n_items, sum(price) as items_value, sum(freight_value) as freight_value
  from {{ ref('stg_order_items') }} group by order_id
),
payments as (
  select order_id, sum(payment_value) as paid_value from {{ ref('stg_order_payments') }} group by order_id
)
select o.order_id, o.customer_id, c.customer_state, o.order_status, o.purchased_at, o.delivered_at,
       coalesce(i.n_items, 0) as n_items, coalesce(i.items_value, 0) as items_value,
       coalesce(i.freight_value, 0) as freight_value, coalesce(p.paid_value, 0) as paid_value, o.batch_id
from {{ ref('stg_orders') }} o
left join items i using (order_id)
left join payments p using (order_id)
left join {{ ref('stg_customers') }} c using (customer_id)
