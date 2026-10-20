-- The indicators that UDISE+ publishes, computed from gold, one row per year and indicator.
-- Definitions follow the official ones: enrolment includes pre-primary; a zero-enrolment
-- school has no students at all (pre-primary included); "government" = management groups
-- marked is_government in reference/management_codes.json.
with s as (
    select academic_year, is_government, total_tch, single_teacher,
           enr_total + enr_pre_primary as enr_all, enr_total
    from int_school_year
)
select academic_year as year, indicator, value from (
    select academic_year,
        count(*)                                        as schools,
        sum(enr_all)                                    as enrolment,
        sum(total_tch)                                  as teachers,
        count(*) filter (where enr_all = 0)             as zero_enrolment_schools,
        sum(single_teacher)                             as single_teacher_schools,
        count(*) filter (where is_government)           as govt_schools,
        sum(enr_all) filter (where is_government)       as govt_enrolment,
        sum(total_tch) filter (where is_government)     as govt_teachers,
        sum(enr_total)                                  as enrolment_classes_1_12
    from s
    group by academic_year
) unpivot (value for indicator in (schools, enrolment, teachers, zero_enrolment_schools,
                                   single_teacher_schools, govt_schools, govt_enrolment,
                                   govt_teachers, enrolment_classes_1_12))
order by year, indicator;
