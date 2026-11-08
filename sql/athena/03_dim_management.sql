-- Athena version of sql/03_dim_management.sql. Key 0 = a code not in the reference.
-- DuckDB -> Athena: null::integer -> cast(null as integer).

-- table: dim_management
select 0 as management_key, cast(null as integer) as management_code,
       cast('Unmapped' as varchar) as management_group, false as is_government
union all
select
    cast(row_number() over (order by code) as integer) as management_key,
    code                                               as management_code,
    management_group,
    is_government
from ref_management
