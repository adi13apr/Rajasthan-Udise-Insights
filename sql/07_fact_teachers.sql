-- fact_teachers
-- Grain: school x year (the source file is already aggregated per school).
-- Measures: teacher counts as published (total, by sex, social group, contract type,
-- qualification, classes taught, training), plus the silver warning flags.
create or replace table fact_teachers as
select
    ds.school_key,
    y.year_key,
    t.* exclude (pseudocode, year, district_file, _source_file, _source_row)
from src_teachers t
join dim_year y    on y.academic_year = t.year
join dim_school ds on ds.pseudocode = t.pseudocode
                  and y.start_year between ds.valid_from_year and ds.valid_to_year;
