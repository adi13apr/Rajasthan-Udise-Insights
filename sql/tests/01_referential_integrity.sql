-- Every foreign key in a fact matches a dimension row (anti-join count must be 0).
select 'fact_enrolment.school_key' as check_name, count(*) as failures
from fact_enrolment f anti join dim_school d using (school_key)
union all select 'fact_enrolment.year_key', count(*) from fact_enrolment f anti join dim_year d using (year_key)
union all select 'fact_enrolment.grade_key', count(*) from fact_enrolment f anti join dim_grade d using (grade_key)
union all select 'fact_enrolment.gender_key', count(*) from fact_enrolment f anti join dim_gender d using (gender_key)
union all select 'fact_enrolment.social_category_key', count(*)
from fact_enrolment f anti join dim_social_category d using (social_category_key)
union all select 'fact_teachers.school_key', count(*) from fact_teachers f anti join dim_school d using (school_key)
union all select 'fact_teachers.year_key', count(*) from fact_teachers f anti join dim_year d using (year_key)
union all select 'fact_school_facilities.school_key', count(*)
from fact_school_facilities f anti join dim_school d using (school_key)
union all select 'fact_school_facilities.year_key', count(*)
from fact_school_facilities f anti join dim_year d using (year_key)
union all select 'dim_school.district_key', count(*) from dim_school s anti join dim_district d using (district_key)
union all select 'dim_school.management_key', count(*)
from dim_school s anti join dim_management d using (management_key);
