-- District marts, from int_school_year (so only from the star schema).
--   mart_district_year             grain: district x year
--   mart_district_year_management  grain: district x year x management group
-- Both come from ONE query with GROUPING SETS, so every indicator has exactly one
-- definition. Schools with no students in classes 1-12 are excluded (as before).
-- Ratios are NULL when the denominator is 0. Values rounded to 3 decimals.
create or replace temp table _district_metrics as
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
        avg(infra_index)                                 as avg_infra_index,
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
        enr_girls / nullif(enrolment, 0)                         as girls_share,
        enr_primary_g / nullif(enr_primary, 0)                   as girls_share_primary,
        enr_secondary_plus_g / nullif(enr_secondary_plus, 0)     as girls_share_sec_plus,
        enr_st / nullif(enrolment, 0)                            as st_share,
        enr_sc / nullif(enrolment, 0)                            as sc_share,
        enrolment / nullif(teachers, 0)                          as ptr,
        -- Average enrolment per grade in classes 9-12 relative to classes 1-5, per sex.
        -- Lower = more students lost after primary; a high boys' ratio can also mean
        -- boys moving in (coaching hubs).
        (enr_secondary_plus_b / 4.0) / nullif(enr_primary_b / 5.0, 0) as boys_progression,
        (enr_secondary_plus_g / 4.0) / nullif(enr_primary_g / 5.0, 0) as girls_progression
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
    -- Girls' share at secondary+ minus their share at primary: cancels out differences in
    -- the child sex ratio. Negative = girls drop out more after class 8; positive = boys leave more.
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
from shares;

create or replace table mart_district_year as
select * exclude (management_group, is_district_total, is_government)
from _district_metrics where is_district_total
order by district, year;

create or replace table mart_district_year_management as
select * exclude (is_district_total)
from _district_metrics where not is_district_total
order by district, year, management_group;
