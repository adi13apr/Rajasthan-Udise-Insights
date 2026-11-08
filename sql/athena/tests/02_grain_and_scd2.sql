-- Athena version of sql/tests/02_grain_and_scd2.sql: each table has the grain it claims, and
-- dim_school versions are well formed. DuckDB count(distinct (a, b)) -> count of a distinct
-- subquery; is_current::int -> cast(is_current as integer).
select 'fact_enrolment grain unique' as check_name,
       (select count(*) from fact_enrolment)
       - (select count(*) from (select distinct school_key, year_key, grade_key, gender_key, social_category_key
                                from fact_enrolment)) as failures
union all select 'fact_teachers one row per school-year',
       (select count(*) from fact_teachers)
       - (select count(*) from (select distinct school_key, year_key from fact_teachers))
union all select 'fact_school_facilities one row per school-year',
       (select count(*) from fact_school_facilities)
       - (select count(*) from (select distinct school_key, year_key from fact_school_facilities))
union all select 'fact_enrolment has no zero rows', (select count(*) from fact_enrolment where students <= 0)
union all select 'dim_school one current version per school',
       (select count(*) from (select pseudocode from dim_school group by pseudocode
                              having sum(cast(is_current as integer)) <> 1))
union all select 'dim_school versions do not overlap',
       (select count(*) from dim_school a join dim_school b
          on a.pseudocode = b.pseudocode and a.version < b.version and a.valid_to_year >= b.valid_from_year)
union all select 'dim_school versions numbered 1..n without gaps',
       (select count(*) from (select pseudocode from dim_school group by pseudocode
                              having max(version) <> count(*)))
union all select 'dim_school keys unique',
       (select count(*) - count(distinct school_key) from dim_school)
union all select 'dim_district has 41 districts', (select abs(count(*) - 41) from dim_district)
