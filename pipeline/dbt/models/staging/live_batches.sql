{{ config(materialized='ephemeral') }}
select batch_id from {{ source('meta', 'batch_registry') }}
where status in ('clean', 'healed', 'released_unchecked')
