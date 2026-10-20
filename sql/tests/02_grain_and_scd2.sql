-- Each table has the grain it claims, and dim_school versions are well formed.
select 'fact_enrolment grain unique' as check_name, count(*) - count(distinct (school_key, year_key, grade_key,
       gender_key, social_category_key)) as failures from fact_enrolment
union all select 'fact_teachers one row per school-year',
       count(*) - count(distinct (school_key, year_key)) from fact_teachers
union all select 'fact_school_facilities one row per school-year',
       count(*) - count(distinct (school_key, year_key)) from fact_school_facilities
union all select 'fact_enrolment has no zero rows', count(*) from fact_enrolment where students <= 0
union all select 'dim_school one current version per school',
       count(*) from (select pseudocode from dim_school group by pseudocode having sum(is_current::int) <> 1)
union all select 'dim_school versions do not overlap', count(*)
from dim_school a join dim_school b
  on a.pseudocode = b.pseudocode and a.version < b.version and a.valid_to_year >= b.valid_from_year
union all select 'dim_school versions numbered 1..n without gaps', count(*)
from (select pseudocode from dim_school group by pseudocode having max(version) <> count(*))
union all select 'dim_school keys unique', count(*) - count(distinct school_key) from dim_school
union all select 'dim_district has 41 districts', abs(count(*) - 41) from dim_district;
