-- The validation measures of powerbi/MEASURES.md, written in SQL over the exported CSVs
-- (views with the same names as the Power BI tables). One SELECT per DAX measure, built
-- from the star schema facts directly (not from int_school_year), so this is an independent
-- second path to the official indicators. One row per year and indicator.
with school_mgmt as (
    select s.school_key, m.is_government
    from dim_school s join dim_management m using (management_key)
),
by_year as (
    -- Schools = COUNTROWS ( fact_school_facilities )
    select year_key, 'schools' as indicator, count(*) as value
    from fact_school_facilities group by year_key
    union all
    -- Enrolment (incl. pre-primary) = SUM ( fact_enrolment[students] )
    select year_key, 'enrolment', sum(students) from fact_enrolment group by year_key
    union all
    -- Enrolment Classes 1-12 = CALCULATE ( [Enrolment (incl. pre-primary)], dim_grade[grade_key] >= 1 )
    select year_key, 'enrolment_classes_1_12', sum(students)
    from fact_enrolment where grade_key >= 1 group by year_key
    union all
    -- Teachers = SUM ( fact_teachers[total_tch] )
    select year_key, 'teachers', sum(total_tch) from fact_teachers group by year_key
    union all
    -- Single-Teacher Schools = CALCULATE ( COUNTROWS ( fact_teachers ), fact_teachers[total_tch] = 1 )
    select year_key, 'single_teacher_schools', count(*) filter (where total_tch = 1)
    from fact_teachers group by year_key
    union all
    -- Zero-Enrolment Schools = [Schools] - distinct school-years in fact_enrolment
    select f.year_key, 'zero_enrolment_schools',
           count(*) - (select count(distinct e.school_key) from fact_enrolment e where e.year_key = f.year_key)
    from fact_school_facilities f group by f.year_key
    union all
    -- Govt Schools / Govt Enrolment / Govt Teachers = the same with dim_management[is_government] = TRUE
    select f.year_key, 'govt_schools', count(*)
    from fact_school_facilities f join school_mgmt using (school_key)
    where is_government group by f.year_key
    union all
    select e.year_key, 'govt_enrolment', sum(students)
    from fact_enrolment e join school_mgmt using (school_key)
    where is_government group by e.year_key
    union all
    select t.year_key, 'govt_teachers', sum(total_tch)
    from fact_teachers t join school_mgmt using (school_key)
    where is_government group by t.year_key
)
select y.academic_year as year, b.indicator, b.value
from by_year b join dim_year y using (year_key)
order by year, indicator
