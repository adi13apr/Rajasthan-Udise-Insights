-- The district-indicator and resource-gap measures of powerbi/MEASURES.md, written in SQL
-- over the exported CSVs (views with the same names as the Power BI tables).
-- One row per district and year, plus one state row per year (district = NULL):
-- GROUPING SETS gives both from one definition, as REMOVEFILTERS ( dim_district ) does in DAX.
-- Like the marts, district indicators count only schools with students in classes 1-12.
with norm as (
    select stage, pupils_per_teacher::double as ppt, min_teachers from norms
),
school_year as (
    select
        a.*,
        d.district_name,
        y.academic_year,
        -- teachers_required (calculated column): per stage with students,
        -- max(min_teachers, roundup(students / pupils_per_teacher))
          (case when a.enr_primary > 0 then greatest((select min_teachers from norm where stage = 'primary'),
                ceil(a.enr_primary / (select ppt from norm where stage = 'primary'))) else 0 end)
        + (case when a.enr_upper_primary > 0 then greatest((select min_teachers from norm where stage = 'upper_primary'),
                ceil(a.enr_upper_primary / (select ppt from norm where stage = 'upper_primary'))) else 0 end)
        + (case when a.enr_secondary > 0 then greatest((select min_teachers from norm where stage = 'secondary'),
                ceil(a.enr_secondary / (select ppt from norm where stage = 'secondary'))) else 0 end)
        + (case when a.enr_higher_secondary > 0 then greatest((select min_teachers from norm where stage = 'higher_secondary'),
                ceil(a.enr_higher_secondary / (select ppt from norm where stage = 'higher_secondary'))) else 0 end)
          as teachers_required,
        -- teachers_for_norms (calculated column): teachers minus those teaching only pre-primary
        a.teachers - coalesce(a.teachers_pre_primary_only, 0) as teachers_for_norms
    from agg_school_year a
    join dim_school s   using (school_key)
    join dim_district d on d.district_key = s.district_key
    join dim_year y     on y.year_key = a.year_key
),
sums as (
    select
        district_name,
        academic_year,
        count(*)                                                 as schools_with_students,
        sum(enr_1_12)                                            as enrolment_1_12,
        sum(teachers)                                            as teachers,
        sum(single_teacher)                                      as single_teacher_schools,
        avg(infra_index)                                         as avg_infra_index,
        sum(enr_girls_1_12)                                      as enr_girls,
        sum(enr_primary)                                         as enr_primary,
        sum(enr_primary_g)                                       as enr_primary_g,
        sum(enr_secondary_plus)                                  as enr_secondary_plus,
        sum(enr_secondary_plus_g)                                as enr_secondary_plus_g,
        avg(has_electricity)                                     as pct_has_electricity,
        avg(has_safe_water)                                      as pct_has_safe_water,
        avg(has_girls_toilet)                                    as pct_has_girls_toilet,
        sum(teachers_required)                                   as teachers_required,
        sum(teachers_for_norms)                                  as teachers_for_norms,
        sum(greatest(0, teachers_required - teachers_for_norms)) as teacher_shortfall,
        sum(greatest(0, teachers_for_norms - teachers_required)) as teacher_surplus,
        count(*) filter (where teachers_for_norms < teachers_required) as schools_short_of_teachers,
        sum(greatest(0, teachers - classrooms))                  as classroom_shortfall,
        sum(greatest(0, teachers - classrooms_usable))           as classroom_shortfall_usable
    from school_year
    where enr_1_12 > 0
    group by grouping sets ((academic_year), (district_name, academic_year))
)
select
    district_name as district,
    academic_year as year,
    schools_with_students,
    enrolment_1_12,
    teachers,
    enrolment_1_12 / nullif(teachers, 0)                         as ptr,
    single_teacher_schools / schools_with_students               as pct_single_teacher,
    avg_infra_index,
    enr_girls / nullif(enrolment_1_12, 0)                        as girls_share,
    enr_secondary_plus_g / nullif(enr_secondary_plus, 0)
        - enr_primary_g / nullif(enr_primary, 0)                 as girls_retention_gap,
    pct_has_electricity,
    pct_has_safe_water,
    pct_has_girls_toilet,
    teachers_required,
    teacher_shortfall,
    teacher_surplus,
    teachers_required - teachers_for_norms                       as net_teacher_gap,
    teacher_shortfall / nullif(teachers_required, 0)             as teacher_shortfall_pct,
    schools_short_of_teachers,
    classroom_shortfall,
    classroom_shortfall_usable
from sums
order by year, district nulls first
