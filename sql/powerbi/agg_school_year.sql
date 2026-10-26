-- agg_school_year: helper table for Power BI, one row per school and year.
-- Built only from gold (int_school_year + the facts), so it adds no new data: it puts the
-- numbers that the resource-gap measures compare SCHOOL BY SCHOOL (enrolment by stage,
-- teachers, classrooms) on one row. In DAX that is then a fast SUMX over 2 lakh rows
-- instead of a lookup into 64 lakh enrolment rows for every school.
-- Same definitions as the marts: enr_1_12 = classes 1-12, teachers = total_tch,
-- infra_index and single_teacher copied from int_school_year.
-- No school code (pseudocode) and no names: school_key and year_key join to the dimensions.
with stage as (
    select
        f.school_key,
        f.year_key,
        sum(f.students) filter (where g.stage = 'upper_primary')    as enr_upper_primary,
        sum(f.students) filter (where g.stage = 'secondary')        as enr_secondary,
        sum(f.students) filter (where g.stage = 'higher_secondary') as enr_higher_secondary
    from fact_enrolment f
    join dim_grade g using (grade_key)
    group by f.school_key, f.year_key
)
select
    i.school_key,
    i.year_key,
    i.enr_pre_primary,
    i.enr_total                                   as enr_1_12,
    i.enr_girls                                   as enr_girls_1_12,
    i.enr_primary,
    i.enr_primary_g,
    coalesce(e.enr_upper_primary, 0)              as enr_upper_primary,
    coalesce(e.enr_secondary, 0)                  as enr_secondary,
    coalesce(e.enr_higher_secondary, 0)           as enr_higher_secondary,
    i.enr_secondary_plus,                         -- classes 9-12
    i.enr_secondary_plus_g,
    i.total_tch                                   as teachers,
    t.class_taugt_pre_primary_only                as teachers_pre_primary_only,
    i.single_teacher,
    f.classrooms,                                 -- good + minor repair + major repair
    f.classrooms_good + f.classrooms_needs_minor_repair as classrooms_usable,
    i.infra_index,
    i.has_electricity, i.has_safe_water, i.has_girls_toilet, i.has_boys_toilet, i.has_handwash,
    i.has_library, i.has_playground, i.has_ramp, i.has_internet, i.has_computer
from int_school_year i
left join stage e                  on e.school_key = i.school_key and e.year_key = i.year_key
left join fact_teachers t          on t.school_key = i.school_key and t.year_key = i.year_key
left join fact_school_facilities f on f.school_key = i.school_key and f.year_key = i.year_key
order by i.school_key, i.year_key
