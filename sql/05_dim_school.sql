-- dim_school: Slowly Changing Dimension Type 2.
-- Grain: one row per VERSION of a school's attributes. A new version starts in the
-- first year where any tracked attribute differs from the year before:
--   district, block, management code, rural/urban, school category, school type.
-- This is how boundary changes are handled: the schools moved from ALWAR to
-- KOTPUTLI-BEHROR get two versions, and each year's facts join to the version valid
-- that year (fact year between valid_from_year and valid_to_year).
-- valid_to_year = last year observed in that version; is_current = the school's latest version.
create or replace table dim_school as
with yearly as (
    select
        s.pseudocode,
        cast(left(s.year, 4) as integer) as start_year,
        d.district_key,
        s.block,
        s.block_clean,
        coalesce(m.management_key, 0)    as management_key,
        s.rural_urban,
        s.location,
        s.school_category,
        s.school_type,
        s.school_type_label,
        -- one value that changes whenever any tracked attribute changes
        md5(concat_ws('|', d.district_key, s.block, s.managment, s.rural_urban,
                      s.school_category, s.school_type)) as attr_hash
    from src_schools s
    join dim_district d on d.district_name = s.district
    left join dim_management m on m.management_code = s.managment
),
changes as (
    select *,
        case when lag(attr_hash) over w is distinct from attr_hash then 1 else 0 end as is_new_version
    from yearly
    window w as (partition by pseudocode order by start_year)
),
numbered as (
    select *, sum(is_new_version) over (partition by pseudocode order by start_year) as version
    from changes
),
versions as (
    select
        pseudocode, version,
        min(start_year) as valid_from_year,
        max(start_year) as valid_to_year,
        -- attributes are equal within a version; take them from its last year
        arg_max(district_key, start_year)      as district_key,
        arg_max(block, start_year)             as block,
        arg_max(block_clean, start_year)       as block_clean,
        arg_max(management_key, start_year)    as management_key,
        arg_max(rural_urban, start_year)       as rural_urban,
        arg_max(location, start_year)          as location,
        arg_max(school_category, start_year)   as school_category,
        arg_max(school_type, start_year)       as school_type,
        arg_max(school_type_label, start_year) as school_type_label
    from numbered
    group by pseudocode, version
)
select
    cast(row_number() over (order by pseudocode, version) as integer) as school_key,
    pseudocode,
    cast(version as integer) as version,
    valid_from_year,
    valid_to_year,
    version = max(version) over (partition by pseudocode) as is_current,
    district_key, block, block_clean, management_key, rural_urban, location,
    school_category, school_type, school_type_label
from versions
order by school_key;
