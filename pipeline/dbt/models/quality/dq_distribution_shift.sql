-- Kengetallen die sterk afwijken van recente goede batches, terwijl elke losse waarde nog binnen de regels valt.
with recent as (
  select metric, value, row_number() over (partition by metric order by batch_id desc) as rn
  from {{ source('landing', 'history_metrics') }}
),
baseline as (
  select metric, median(value) as med, count(*) as n from recent where rn <= 4 group by metric
),
current as (
  select 'avg_price' as metric, 'order_items' as table_name, 'price' as column_name,
         avg(try_cast(price as double)) as value, count(*) as n_rows
  from {{ source('landing', 'order_items') }}
)
select 'distribution_shift' as issue_type, c.table_name, c.column_name, c.metric || '_shift' as check_name,
       c.n_rows as n_affected, c.n_rows as n_total, null::varchar as sample,
       'gemiddelde (' || round(c.value, 2)::varchar || ') is ' || round(c.value / b.med, 2)::varchar
         || 'x de mediaan (' || round(b.med, 2)::varchar || ') van de laatste ' || b.n::varchar || ' goede batches' as details,
       null::varchar as related_table
from current c
join baseline b using (metric)
where b.n >= {{ var('volume_min_history') }} and b.med > 0 and c.value is not null
  and (c.value / b.med < {{ var('shift_low') }} or c.value / b.med > {{ var('shift_high') }})
