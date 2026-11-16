-- Bedrijfsregels op waarden die wel leesbaar zijn (onleesbare waarden vallen onder schema_drift).
with rules as (select * from {{ ref('dq_invalid_rows') }})
select 'invalid_values' as issue_type, table_name, column_name, check_name,
       count(*) filter (where bad) as n_affected, count(*) as n_total,
       to_json(list(coalesce(k, '<leeg>')) filter (where bad)[1:5])::varchar as sample,
       any_value(details) as details, null::varchar as related_table
from rules
group by table_name, column_name, check_name
having count(*) filter (where bad) > 0
