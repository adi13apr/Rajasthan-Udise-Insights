-- Nothing is lost or double counted between silver and gold.
-- Silver enrolment is summed straight from its wide columns (whatever grade/gender
-- columns exist that year), independently of the UNPIVOT used to build the fact.
select 'enrolment total silver = gold' as check_name,
       abs((select sum(list_sum(list_value(*columns('^c(pp|[0-9]+)_[bgt]$')))) from src_enrolment)
           - (select sum(students) from fact_enrolment)) as failures
union all select 'teachers total silver = gold',
       abs((select sum(total_tch) from src_teachers) - (select sum(total_tch) from fact_teachers))
union all select 'school-years silver = gold',
       abs((select count(*) from src_schools) - (select count(*) from fact_school_facilities))
union all select 'teacher rows silver = gold',
       abs((select count(*) from src_teachers) - (select count(*) from fact_teachers))
union all select 'schools silver = current versions in dim_school',
       abs((select count(distinct pseudocode) from src_schools)
           - (select count(*) from dim_school where is_current));
