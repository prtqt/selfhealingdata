select customer_id, customer_unique_id, customer_zip_code_prefix, customer_city, customer_state, batch_id
from {{ source('raw', 'customers') }}
where batch_id in (select batch_id from {{ ref('live_batches') }})
qualify row_number() over (partition by customer_id order by batch_id desc) = 1
