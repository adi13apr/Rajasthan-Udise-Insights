-- Athena version of sql/07_fact_teachers.sql. Grain: school x year.
-- DuckDB -> Athena: t.* exclude (...) does not exist, so aws/athena_build.py fills in
-- {{teacher_columns}}: every silver_teachers column except pseudocode, year, district_file,
-- _source_file and _source_row (the same ones DuckDB excludes).

-- table: fact_teachers partitioned_by=year_key
select
    ds.school_key,
    {{teacher_columns}},
    y.year_key
from silver_teachers t
join dim_year y    on y.academic_year = t.year
join dim_school ds on ds.pseudocode = t.pseudocode
                  and y.start_year between ds.valid_from_year and ds.valid_to_year
