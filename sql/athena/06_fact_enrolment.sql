-- Athena version of sql/06_fact_enrolment.sql.
-- Grain: school x year x grade x gender x social category; rows with 0 students omitted.
-- DuckDB -> Athena: UNPIVOT ... columns('^c(pp|[0-9]+)_[bgt]$') has no Athena equivalent, so
-- aws/athena_build.py fills in the enrolment columns silver_enrolment really has
-- ({{enrolment_column_names}} and {{enrolment_columns}}) and UNNEST turns them into rows
-- (UNNEST of two arrays pairs them up). Missing (NULL) cells drop out at students > 0, as
-- UNPIVOT skips NULLs. Partitioned by year_key (a partition column must come last).

-- table: fact_enrolment partitioned_by=year_key
with long as (
    select e.pseudocode, e.year, e.item_id, u.source_column, u.students
    from silver_enrolment e
    cross join unnest(array[{{enrolment_column_names}}],
                      array[{{enrolment_columns}}]) as u(source_column, students)
),
parsed as (
    select
        pseudocode,
        year,
        item_id,
        regexp_extract(source_column, '^c(pp|[0-9]+)_', 1)        as grade,
        upper(substr(source_column, length(source_column)))      as gender_code,
        students
    from long
    where students > 0
)
select
    ds.school_key,
    g.grade_key,
    ge.gender_key,
    sc.social_category_key,
    cast(p.students as integer) as students,
    y.year_key
from parsed p
join dim_year y             on y.academic_year = p.year
join dim_school ds          on ds.pseudocode = p.pseudocode
                           and y.start_year between ds.valid_from_year and ds.valid_to_year
join dim_grade g            on g.grade_code = upper(p.grade)
join dim_gender ge          on ge.gender_code = p.gender_code
join dim_social_category sc on sc.item_id = p.item_id
