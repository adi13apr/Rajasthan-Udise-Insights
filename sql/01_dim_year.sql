-- dim_year: one row per academic year in silver.
-- Grain: academic year. Key: row number in year order (deterministic: same data -> same keys).
create or replace table dim_year as
select
    cast(row_number() over (order by year) as integer) as year_key,
    year                                               as academic_year,   -- e.g. '2024-25'
    cast(left(year, 4) as integer)                     as start_year       -- e.g. 2024
from (select distinct year from src_schools);
