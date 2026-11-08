-- _source_row runs 1..n in every bronze file (no row lost or repeated since the file was made).
-- The validator Lambda checks only the Parquet footer (the AWS SDK for pandas layer cannot read
-- zstd data pages), so this check, which needs the data, runs here on the whole bronze tables.
-- Per file (year, _source_file): count(*) = count(distinct _source_row) = max(_source_row) and
-- min(_source_row) = 1. A NULL _source_row is not counted by count(distinct), so it fails too.
-- failures = number of files that break it. Must be 0.
select 'bronze_profile1: _source_row is 1..n in every file' as check_name, count(*) as failures
from (select year, "_source_file", count(*) as n, count(distinct "_source_row") as d,
             min("_source_row") as lo, max("_source_row") as hi
      from bronze_profile1 group by year, "_source_file") f
where not (n = d and d = hi and lo = 1)
union all
select 'bronze_enrolment1: _source_row is 1..n in every file', count(*)
from (select year, "_source_file", count(*) as n, count(distinct "_source_row") as d,
             min("_source_row") as lo, max("_source_row") as hi
      from bronze_enrolment1 group by year, "_source_file") f
where not (n = d and d = hi and lo = 1)
union all
select 'bronze_facility: _source_row is 1..n in every file', count(*)
from (select year, "_source_file", count(*) as n, count(distinct "_source_row") as d,
             min("_source_row") as lo, max("_source_row") as hi
      from bronze_facility group by year, "_source_file") f
where not (n = d and d = hi and lo = 1)
union all
select 'bronze_teacher: _source_row is 1..n in every file', count(*)
from (select year, "_source_file", count(*) as n, count(distinct "_source_row") as d,
             min("_source_row") as lo, max("_source_row") as hi
      from bronze_teacher group by year, "_source_file") f
where not (n = d and d = hi and lo = 1)
