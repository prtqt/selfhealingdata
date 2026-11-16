-- Producten komen in elke batch opnieuw mee; de laatste aanlevering telt.
select product_id, product_category_name, try_cast(product_weight_g as double) as weight_g, batch_id
from {{ source('raw', 'products') }}
where batch_id in (select batch_id from {{ ref('live_batches') }})
qualify row_number() over (partition by product_id order by batch_id desc) = 1
