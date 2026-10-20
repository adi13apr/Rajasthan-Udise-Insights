-- Small lookup dimensions for the enrolment fact.

-- dim_grade: pre-primary and classes 1-12 with their stage (stages as in config.STAGES).
create or replace table dim_grade as
select
    grade_key,
    case when grade_key = 0 then 'PP' else cast(grade_key as varchar) end as grade_code,
    case when grade_key = 0 then 'Pre-primary' else 'Class ' || grade_key end as grade_label,
    case when grade_key = 0 then 'pre_primary'
         when grade_key between 1 and 5 then 'primary'
         when grade_key between 6 and 8 then 'upper_primary'
         when grade_key between 9 and 10 then 'secondary'
         else 'higher_secondary' end as stage
from range(0, 13) t(grade_key);

-- dim_gender: the suffix of the enrolment columns (_b, _g, _t).
-- Transgender columns exist only from 2025-26; in 2024-25 boys include transgender (schema remark).
create or replace table dim_gender as
select * from (values (1, 'B', 'Boys'), (2, 'G', 'Girls'), (3, 'T', 'Transgender'))
    t(gender_key, gender_code, gender_label);

-- dim_social_category: Enrolment_1 item_group 1 (a complete split of all students).
create or replace table dim_social_category as
select * from (values (1, 1, 'General'), (2, 2, 'SC'), (3, 3, 'ST'), (4, 4, 'OBC'))
    t(social_category_key, item_id, social_category);
