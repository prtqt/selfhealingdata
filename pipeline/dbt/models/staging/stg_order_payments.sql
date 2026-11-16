select order_id, try_cast(payment_sequential as integer) as payment_sequential, payment_type,
       try_cast(payment_installments as integer) as payment_installments,
       try_cast(payment_value as double) as payment_value, batch_id
from {{ source('raw', 'order_payments') }}
where batch_id in (select batch_id from {{ ref('live_batches') }})
