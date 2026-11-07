-- Athena version of sql/11_marts_district.sql. Both marts come from ONE GROUPING SETS query,
-- here a view (int_district_metrics, dropped after the marts are built) instead of a temp table.
-- DuckDB -> Athena: integer / integer is a whole-number division in Athena (DuckDB gives a
-- DOUBLE), so every ratio casts to double first; `x::double` -> cast(x as double);
-- `* exclude (...)` -> the columns written out.

-- view: int_district_metrics
with sums as (
    select
        district_name,
        academic_year,
        management_group,
        grouping(management_group) = 1                   as is_district_total,
        bool_and(is_government)                          as is_government,
        count(*)                                         as schools,
        sum(enr_total)                                   as enrolment,
        sum(enr_girls)                                   as enr_girls,
        sum(enr_primary)                                 as enr_primary,
        sum(enr_primary_b)                               as enr_primary_b,
        sum(enr_primary_g)                               as enr_primary_g,
        sum(enr_secondary_plus)                          as enr_secondary_plus,
        sum(enr_secondary_plus_b)                        as enr_secondary_plus_b,
        sum(enr_secondary_plus_g)                        as enr_secondary_plus_g,
        sum(enr_sc)                                      as enr_sc,
        sum(enr_st)                                      as enr_st,
        sum(total_tch)                                   as teachers,
        avg(single_teacher) * 100                        as pct_single_teacher,
        -- exact decimal sum, then one division (same result on every run)
        cast(sum(infra_index) as double) / count(infra_index) as avg_infra_index,
        avg(has_electricity) * 100  as pct_has_electricity,
        avg(has_safe_water) * 100   as pct_has_safe_water,
        avg(has_girls_toilet) * 100 as pct_has_girls_toilet,
        avg(has_boys_toilet) * 100  as pct_has_boys_toilet,
        avg(has_handwash) * 100     as pct_has_handwash,
        avg(has_library) * 100      as pct_has_library,
        avg(has_playground) * 100   as pct_has_playground,
        avg(has_ramp) * 100         as pct_has_ramp,
        avg(has_internet) * 100     as pct_has_internet,
        avg(has_computer) * 100     as pct_has_computer
    from int_school_year
    where enr_total > 0
    group by grouping sets ((district_name, academic_year),
                            (district_name, academic_year, management_group))
),
shares as (
    select *,
        cast(enr_girls as double) / nullif(enrolment, 0)                     as girls_share,
        cast(enr_primary_g as double) / nullif(enr_primary, 0)               as girls_share_primary,
        cast(enr_secondary_plus_g as double) / nullif(enr_secondary_plus, 0) as girls_share_sec_plus,
        cast(enr_st as double) / nullif(enrolment, 0)                        as st_share,
        cast(enr_sc as double) / nullif(enrolment, 0)                        as sc_share,
        cast(enrolment as double) / nullif(teachers, 0)                      as ptr,
        (cast(enr_secondary_plus_b as double) / 4) / nullif(cast(enr_primary_b as double) / 5, 0) as boys_progression,
        (cast(enr_secondary_plus_g as double) / 4) / nullif(cast(enr_primary_g as double) / 5, 0) as girls_progression
    from sums
)
select
    district_name as district, academic_year as year, management_group, is_district_total,
    is_government, schools, enrolment,
    round(girls_share, 3)          as girls_share,
    round(girls_share_primary, 3)  as girls_share_primary,
    round(girls_share_sec_plus, 3) as girls_share_sec_plus,
    round(st_share, 3)             as st_share,
    round(sc_share, 3)             as sc_share,
    teachers,
    round(ptr, 3)                  as ptr,
    round(pct_single_teacher, 3)   as pct_single_teacher,
    round(avg_infra_index, 3)      as avg_infra_index,
    round(girls_share_sec_plus - girls_share_primary, 3) as girls_retention_gap,
    round(boys_progression, 3)     as boys_progression,
    round(girls_progression, 3)    as girls_progression,
    round(pct_has_electricity, 3)  as pct_has_electricity,
    round(pct_has_safe_water, 3)   as pct_has_safe_water,
    round(pct_has_girls_toilet, 3) as pct_has_girls_toilet,
    round(pct_has_boys_toilet, 3)  as pct_has_boys_toilet,
    round(pct_has_handwash, 3)     as pct_has_handwash,
    round(pct_has_library, 3)      as pct_has_library,
    round(pct_has_playground, 3)   as pct_has_playground,
    round(pct_has_ramp, 3)         as pct_has_ramp,
    round(pct_has_internet, 3)     as pct_has_internet,
    round(pct_has_computer, 3)     as pct_has_computer
from shares

-- table: mart_district_year
select district, year, schools, enrolment, girls_share, girls_share_primary, girls_share_sec_plus,
       st_share, sc_share, teachers, ptr, pct_single_teacher, avg_infra_index, girls_retention_gap,
       boys_progression, girls_progression, pct_has_electricity, pct_has_safe_water,
       pct_has_girls_toilet, pct_has_boys_toilet, pct_has_handwash, pct_has_library,
       pct_has_playground, pct_has_ramp, pct_has_internet, pct_has_computer
from int_district_metrics
where is_district_total

-- table: mart_district_year_management
select district, year, management_group, is_government, schools, enrolment, girls_share,
       girls_share_primary, girls_share_sec_plus, st_share, sc_share, teachers, ptr,
       pct_single_teacher, avg_infra_index, girls_retention_gap, boys_progression, girls_progression,
       pct_has_electricity, pct_has_safe_water, pct_has_girls_toilet, pct_has_boys_toilet,
       pct_has_handwash, pct_has_library, pct_has_playground, pct_has_ramp, pct_has_internet,
       pct_has_computer
from int_district_metrics
where not is_district_total
