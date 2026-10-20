-- fact_school_facilities
-- Grain: school x year. One row for every school-year in silver schools (a periodic
-- snapshot), so it is also the table to count schools from. Facility measures are NULL
-- for a school with no facility row (none in the current data).
-- has_* : 1 = available, 0 = not, NULL = not reported / not applicable (built in silver).
create or replace table fact_school_facilities as
select
    ds.school_key,
    y.year_key,
    s.lowclass,
    s.highclass,
    f.has_electricity, f.has_safe_water, f.has_girls_toilet, f.has_boys_toilet,
    f.has_handwash, f.has_library, f.has_playground, f.has_ramp, f.has_internet,
    f.has_computer,
    f.classrooms,                -- rebuilt: good + minor repair + major repair
    f.classrooms_good,
    f.classrooms_needs_minor_repair,
    f.classrooms_needs_major_repair,
    f.pct_classrooms_major_repair,
    f.total_girls_toilet, f.total_girls_func_toilet,
    f.total_boys_toilet, f.total_boys_func_toilet,
    f.flag_invalid_code             as flag_facility_invalid_code,
    f.flag_functional_toilets_exceed_total,
    f.flag_classroom_total_mismatch
from src_schools s
join dim_year y        on y.academic_year = s.year
join dim_school ds     on ds.pseudocode = s.pseudocode
                      and y.start_year between ds.valid_from_year and ds.valid_to_year
left join src_facilities f on f.pseudocode = s.pseudocode and f.year = s.year;
