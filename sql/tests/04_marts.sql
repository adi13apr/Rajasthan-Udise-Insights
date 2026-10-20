-- Marts agree with the star schema and with each other.
select 'int_school_year one row per school-year' as check_name,
       abs((select count(*) from int_school_year) - (select count(*) from fact_school_facilities)) as failures
union all select 'mart_district_year one row per district-year',
       count(*) - count(distinct (district, year)) from mart_district_year
union all select 'mart_district_year covers every district-year with students',
       abs((select count(*) from mart_district_year)
           - (select count(distinct (district_name, academic_year)) from int_school_year where enr_total > 0))
union all select 'mart enrolment = fact enrolment classes 1-12',
       abs((select sum(enrolment) from mart_district_year)
           - (select sum(students) from fact_enrolment where grade_key between 1 and 12))
union all select 'management mart adds up to district mart: schools',
       abs((select sum(schools) from mart_district_year_management) - (select sum(schools) from mart_district_year))
union all select 'management mart adds up to district mart: enrolment',
       abs((select sum(enrolment) from mart_district_year_management) - (select sum(enrolment) from mart_district_year))
union all select 'management mart adds up to district mart: teachers',
       abs((select sum(teachers) from mart_district_year_management) - (select sum(teachers) from mart_district_year))
union all select 'shares between 0 and 1', count(*) from mart_district_year
where not (girls_share between 0 and 1 and st_share between 0 and 1 and sc_share between 0 and 1);
