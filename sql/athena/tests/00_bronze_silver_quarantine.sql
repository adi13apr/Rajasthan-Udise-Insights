-- Every bronze row ends up in silver or in quarantine (pipeline/silver.py reconcile(), here in
-- Athena): bronze rows = silver rows + quarantined rows, per bronze dataset. Must be 0.
select 'profile1: bronze = silver_schools + quarantine' as check_name,
       abs((select count(*) from bronze_profile1)
           - (select count(*) from silver_schools) - (select count(*) from quarantine_schools)) as failures
union all select 'enrolment1: bronze = silver_enrolment(+other) + quarantine',
       abs((select count(*) from bronze_enrolment1)
           - (select count(*) from silver_enrolment) - (select count(*) from quarantine_enrolment)
           - (select count(*) from silver_enrolment_other) - (select count(*) from quarantine_enrolment_other))
union all select 'facility: bronze = silver_facilities + quarantine',
       abs((select count(*) from bronze_facility)
           - (select count(*) from silver_facilities) - (select count(*) from quarantine_facilities))
union all select 'teacher: bronze = silver_teachers + quarantine',
       abs((select count(*) from bronze_teacher)
           - (select count(*) from silver_teachers) - (select count(*) from quarantine_teachers))
