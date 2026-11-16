select order_id, customer_id, order_status,
       try_cast(order_purchase_timestamp as timestamp) as purchased_at,
       try_cast(order_approved_at as timestamp) as approved_at,
       try_cast(order_delivered_customer_date as timestamp) as delivered_at,
       try_cast(order_estimated_delivery_date as timestamp) as estimated_delivery_at,
       batch_id
from {{ source('raw', 'orders') }}
where batch_id in (select batch_id from {{ ref('live_batches') }})
