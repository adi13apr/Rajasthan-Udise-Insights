-- Athena version of sql/05_dim_school.sql: Slowly Changing Dimension Type 2.
-- One row per version of a school's attributes; a new version starts in the first year where
-- district, block, management code, rural/urban, school category or school type changes.
-- DuckDB -> Athena: md5(text) -> to_hex(md5(to_utf8(text))) (concat_ws needs text arguments);
-- arg_max -> max_by; the named WINDOW clause is written out in each OVER; left() -> substr().

-- table: dim_school
with yearly as (
    select
        s.pseudocode,
        cast(substr(s.year, 1, 4) as integer) as start_year,
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
        to_hex(md5(to_utf8(concat_ws('|', cast(d.district_key as varchar), s.block,
                                     cast(s.managment as varchar), cast(s.rural_urban as varchar),
                                     cast(s.school_category as varchar), cast(s.school_type as varchar)))))
            as attr_hash
    from silver_schools s
    join dim_district d on d.district_name = s.district
    left join dim_management m on m.management_code = s.managment
),
changes as (
    select *,
        case when lag(attr_hash) over (partition by pseudocode order by start_year) is distinct from attr_hash
             then 1 else 0 end as is_new_version
    from yearly
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
        max_by(district_key, start_year)      as district_key,
        max_by(block, start_year)             as block,
        max_by(block_clean, start_year)       as block_clean,
        max_by(management_key, start_year)    as management_key,
        max_by(rural_urban, start_year)       as rural_urban,
        max_by(location, start_year)          as location,
        max_by(school_category, start_year)   as school_category,
        max_by(school_type, start_year)       as school_type,
        max_by(school_type_label, start_year) as school_type_label
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
