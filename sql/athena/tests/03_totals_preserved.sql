-- Athena version of sql/tests/03_totals_preserved.sql: nothing lost or double counted between
-- silver and gold. DuckDB list_sum(list_value(*columns(regex))) -> {{enrolment_row_sum}}, the
-- sum of every enrolment column silver_enrolment has (filled in by aws/athena_build.py),
-- independent of the UNNEST used to build the fact.
select 'enrolment total silver = gold' as check_name,
       abs((select sum({{enrolment_row_sum}}) from silver_enrolment)
           - (select sum(students) from fact_enrolment)) as failures
union all select 'teachers total silver = gold',
       abs((select sum(total_tch) from silver_teachers) - (select sum(total_tch) from fact_teachers))
union all select 'school-years silver = gold',
       abs((select count(*) from silver_schools) - (select count(*) from fact_school_facilities))
union all select 'teacher rows silver = gold',
       abs((select count(*) from silver_teachers) - (select count(*) from fact_teachers))
union all select 'schools silver = current versions in dim_school',
       abs((select count(distinct pseudocode) from silver_schools)
           - (select count(*) from dim_school where is_current))
