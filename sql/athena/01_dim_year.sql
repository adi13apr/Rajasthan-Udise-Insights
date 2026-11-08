-- Athena (Trino) version of sql/01_dim_year.sql. Run by aws/athena_build.py: each
-- "-- table: <name>" block becomes CREATE TABLE <name> AS <select> (Parquet under gold/<name>/).
-- DuckDB -> Athena: left(year, 4) -> substr(year, 1, 4).

-- table: dim_year
select
    cast(row_number() over (order by year) as integer) as year_key,
    year                                               as academic_year,   -- e.g. '2024-25'
    cast(substr(year, 1, 4) as integer)                as start_year       -- e.g. 2024
from (select distinct year from silver_schools)
