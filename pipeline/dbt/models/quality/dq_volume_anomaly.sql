-- Ontbrekende of lege tabellen, en rijaantallen die sterk afwijken van recente goede batches.
with recent as (
  select table_name, row_count,
         row_number() over (partition by table_name order by batch_id desc) as rn
  from {{ source('landing', 'history') }}
),
baseline as (
  select table_name, median(row_count) as med, count(*) as n
  from recent where rn <= 4 group by table_name
)
select 'volume_anomaly' as issue_type, f.table_name, null::varchar as column_name,
       case when not f.present then 'missing_table'
            when f.row_count = 0 then 'empty_table'
            else 'row_count_deviation' end as check_name,
       f.row_count as n_affected, f.row_count as n_total, null::varchar as sample,
       case when not f.present then 'bestand ontbreekt in de aanlevering'
            when f.row_count = 0 then 'tabel bevat geen rijen'
            else 'aantal rijen is ' || round(f.row_count / b.med, 2)::varchar || 'x de mediaan (' || b.med::bigint::varchar
                 || ') van de laatste ' || b.n::varchar || ' goede batches' end as details,
       null::varchar as related_table
from {{ source('landing', 'files') }} f
left join baseline b on b.table_name = f.table_name
where not f.present
   or f.row_count = 0
   or (b.n >= {{ var('volume_min_history') }} and b.med > 0
       and (f.row_count / b.med < {{ var('volume_low') }} or f.row_count / b.med > {{ var('volume_high') }}))
