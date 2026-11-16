select order_id, try_cast(order_item_id as integer) as order_item_id, product_id, seller_id,
       try_cast(price as double) as price, try_cast(freight_value as double) as freight_value, batch_id
from {{ source('raw', 'order_items') }}
where batch_id in (select batch_id from {{ ref('live_batches') }})
