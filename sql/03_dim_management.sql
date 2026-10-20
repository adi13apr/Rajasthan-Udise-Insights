-- dim_management: UDISE+ management codes and their validated grouping
-- (reference/management_codes.json). Grain: management code.
-- Key 0 = unknown code (a school whose code is not in the reference; flagged in silver).
create or replace table dim_management as
select 0 as management_key, null::integer as management_code, 'Unmapped' as management_group,
       false as is_government
union all
select
    cast(row_number() over (order by code) as integer) as management_key,
    code                                               as management_code,
    management_group,
    is_government
from ref_management
order by management_key;
