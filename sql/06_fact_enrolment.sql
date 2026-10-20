-- fact_enrolment
-- Grain: school x year x grade x gender x social category.
-- Source: silver enrolment (Enrolment_1 item_group 1 = General/SC/ST/OBC, a complete
-- split of all students, so summing it gives totals without double counting).
-- Measure: students. Rows with 0 students are omitted (about 9 in 10 cells are 0);
-- a missing row means 0. Transgender (T) exists only from 2025-26 (NULL columns in
-- 2024-25 are skipped by UNPIVOT; that year's boys include transgender students).
create or replace table fact_enrolment as
with long as (
    unpivot src_enrolment
    on columns('^c(pp|[0-9]+)_[bgt]$')
    into name source_column value students
),
parsed as (
    select
        pseudocode,
        year,
        item_id,
        regexp_extract(source_column, '^c(pp|[0-9]+)_', 1) as grade,
        upper(right(source_column, 1))                    as gender_code,
        students
    from long
    where students > 0
)
select
    ds.school_key,
    y.year_key,
    g.grade_key,
    ge.gender_key,
    sc.social_category_key,
    cast(p.students as integer) as students
from parsed p
join dim_year y             on y.academic_year = p.year
join dim_school ds          on ds.pseudocode = p.pseudocode
                           and y.start_year between ds.valid_from_year and ds.valid_to_year
join dim_grade g            on g.grade_code = upper(p.grade)
join dim_gender ge          on ge.gender_code = p.gender_code
join dim_social_category sc on sc.item_id = p.item_id;
