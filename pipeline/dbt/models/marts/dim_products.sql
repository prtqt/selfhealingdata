select product_id, coalesce(product_category_name, 'onbekend') as category, weight_g
from {{ ref('stg_products') }}
