-- Athena version of sql/04_dim_lookups.sql: small lookup dimensions for the enrolment fact.
-- DuckDB -> Athena: range(0, 13) -> unnest(sequence(0, 12)).

-- table: dim_grade
select
    grade_key,
    case when grade_key = 0 then 'PP' else cast(grade_key as varchar) end as grade_code,
    case when grade_key = 0 then 'Pre-primary' else 'Class ' || cast(grade_key as varchar) end as grade_label,
    case when grade_key = 0 then 'pre_primary'
         when grade_key between 1 and 5 then 'primary'
         when grade_key between 6 and 8 then 'upper_primary'
         when grade_key between 9 and 10 then 'secondary'
         else 'higher_secondary' end as stage
from unnest(sequence(0, 12)) as t(grade_key)

-- table: dim_gender
select cast(gender_key as integer) as gender_key, cast(gender_code as varchar) as gender_code,
       cast(gender_label as varchar) as gender_label
from (values (1, 'B', 'Boys'), (2, 'G', 'Girls'), (3, 'T', 'Transgender'))
    as t(gender_key, gender_code, gender_label)

-- table: dim_social_category
select cast(social_category_key as integer) as social_category_key, cast(item_id as integer) as item_id,
       cast(social_category as varchar) as social_category
from (values (1, 1, 'General'), (2, 2, 'SC'), (3, 3, 'ST'), (4, 4, 'OBC'))
    as t(social_category_key, item_id, social_category)
