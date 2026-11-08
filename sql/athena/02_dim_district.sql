-- Athena version of sql/02_dim_district.sql: the 41 districts and their older spellings.
-- DuckDB -> Athena: list_sort(list(x) filter ...) -> array_sort(array_agg(x) filter ...);
-- the empty list [] needs a type: cast(array[] as array(varchar)).

-- table: dim_district
select
    cast(row_number() over (order by d.district_name) as integer) as district_key,
    d.district_name,
    coalesce(array_sort(array_agg(a.alias) filter (where a.alias is not null)),
             cast(array[] as array(varchar)))                     as aliases
from ref_districts d
left join ref_district_aliases a on a.district_name = d.district_name
group by d.district_name
