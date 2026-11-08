-- Athena version of sql/validation/01_official_indicators.sql: the indicators UDISE+ publishes,
-- computed from Athena gold, one row per year and indicator (same definitions).
-- DuckDB UNPIVOT -> UNNEST of a name array and a value array (paired up).
with s as (
    select academic_year, is_government, total_tch, single_teacher,
           enr_total + enr_pre_primary as enr_all, enr_total
    from int_school_year
),
agg as (
    select academic_year,
        count(*)                                        as schools,
        sum(enr_all)                                    as enrolment,
        sum(total_tch)                                  as teachers,
        count(*) filter (where enr_all = 0)             as zero_enrolment_schools,
        sum(single_teacher)                             as single_teacher_schools,
        count(*) filter (where is_government)           as govt_schools,
        sum(enr_all) filter (where is_government)       as govt_enrolment,
        sum(total_tch) filter (where is_government)     as govt_teachers,
        sum(enr_total)                                  as enrolment_classes_1_12
    from s
    group by academic_year
)
select academic_year as year, u.indicator, u.value
from agg
cross join unnest(
    array['schools', 'enrolment', 'teachers', 'zero_enrolment_schools', 'single_teacher_schools',
          'govt_schools', 'govt_enrolment', 'govt_teachers', 'enrolment_classes_1_12'],
    array[cast(schools as bigint), cast(enrolment as bigint), cast(teachers as bigint),
          cast(zero_enrolment_schools as bigint), cast(single_teacher_schools as bigint),
          cast(govt_schools as bigint), cast(govt_enrolment as bigint), cast(govt_teachers as bigint),
          cast(enrolment_classes_1_12 as bigint)]) as u(indicator, value)
order by year, indicator
