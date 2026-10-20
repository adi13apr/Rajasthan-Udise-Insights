-- dim_district: the 41 districts of Rajasthan (reference/districts.json).
-- Grain: district. aliases = older spellings used by the portal (e.g. JALOR for JALORE).
create or replace table dim_district as
select
    cast(row_number() over (order by d.district_name) as integer) as district_key,
    d.district_name,
    coalesce(list_sort(list(a.alias) filter (where a.alias is not null)), []) as aliases
from ref_districts d
left join ref_district_aliases a on a.district_name = d.district_name
group by d.district_name;
