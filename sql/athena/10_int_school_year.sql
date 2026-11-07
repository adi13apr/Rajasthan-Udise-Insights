-- Athena version of sql/10_int_school_year.sql: one row per school and year, built only from
-- the star schema. School-level: stays in the private bucket, never exported.
-- DuckDB -> Athena: `x::int` -> cast(x as integer); USING joins -> ON; 100.0 is a DECIMAL in
-- Athena (exact, fixed scale), so the index is computed in DOUBLE (100e0) as in DuckDB.

-- table: int_school_year partitioned_by=year_key
with enrolment as (
    select
        f.school_key,
        f.year_key,
        sum(f.students) filter (where g.grade_key between 1 and 12)                       as enr_total,
        sum(f.students) filter (where g.grade_key between 1 and 12 and ge.gender_code = 'G') as enr_girls,
        sum(f.students) filter (where g.grade_key = 0)                                    as enr_pre_primary,
        sum(f.students) filter (where g.stage = 'primary')                                as enr_primary,
        sum(f.students) filter (where g.stage = 'primary' and ge.gender_code = 'B')       as enr_primary_b,
        sum(f.students) filter (where g.stage = 'primary' and ge.gender_code = 'G')       as enr_primary_g,
        sum(f.students) filter (where g.stage in ('secondary', 'higher_secondary'))       as enr_secondary_plus,
        sum(f.students) filter (where g.stage in ('secondary', 'higher_secondary')
                                  and ge.gender_code = 'B')                               as enr_secondary_plus_b,
        sum(f.students) filter (where g.stage in ('secondary', 'higher_secondary')
                                  and ge.gender_code = 'G')                               as enr_secondary_plus_g,
        sum(f.students) filter (where g.grade_key between 1 and 12 and sc.social_category = 'SC') as enr_sc,
        sum(f.students) filter (where g.grade_key between 1 and 12 and sc.social_category = 'ST') as enr_st
    from fact_enrolment f
    join dim_grade g            on g.grade_key = f.grade_key
    join dim_gender ge          on ge.gender_key = f.gender_key
    join dim_social_category sc on sc.social_category_key = f.social_category_key
    group by f.school_key, f.year_key
)
select
    sf.school_key,
    y.academic_year,
    s.pseudocode,
    s.district_key,
    d.district_name,
    s.management_key,
    m.management_group,
    m.is_government,
    s.location,
    s.school_category,
    coalesce(e.enr_total, 0)             as enr_total,          -- classes 1-12
    coalesce(e.enr_girls, 0)             as enr_girls,
    coalesce(e.enr_pre_primary, 0)       as enr_pre_primary,
    coalesce(e.enr_primary, 0)           as enr_primary,
    coalesce(e.enr_primary_b, 0)         as enr_primary_b,
    coalesce(e.enr_primary_g, 0)         as enr_primary_g,
    coalesce(e.enr_secondary_plus, 0)    as enr_secondary_plus, -- classes 9-12
    coalesce(e.enr_secondary_plus_b, 0)  as enr_secondary_plus_b,
    coalesce(e.enr_secondary_plus_g, 0)  as enr_secondary_plus_g,
    coalesce(e.enr_sc, 0)                as enr_sc,
    coalesce(e.enr_st, 0)                as enr_st,
    t.total_tch,
    cast(coalesce(t.total_tch = 1, false) as integer) as single_teacher,
    sf.has_electricity, sf.has_safe_water, sf.has_girls_toilet, sf.has_boys_toilet,
    sf.has_handwash, sf.has_library, sf.has_playground, sf.has_ramp, sf.has_internet,
    sf.has_computer,
    -- Infrastructure index: share of 8 basic facilities present (0-100), over the facilities
    -- reported. Playground left out (reporting change, DATA_QUALITY_LOG #17).
    cast(round(100e0 * (coalesce(sf.has_electricity, 0) + coalesce(sf.has_safe_water, 0)
                   + coalesce(sf.has_girls_toilet, 0) + coalesce(sf.has_handwash, 0)
                   + coalesce(sf.has_library, 0) + coalesce(sf.has_ramp, 0)
                   + coalesce(sf.has_internet, 0) + coalesce(sf.has_computer, 0))
          / nullif(cast(sf.has_electricity is not null as integer) + cast(sf.has_safe_water is not null as integer)
                   + cast(sf.has_girls_toilet is not null as integer) + cast(sf.has_handwash is not null as integer)
                   + cast(sf.has_library is not null as integer) + cast(sf.has_ramp is not null as integer)
                   + cast(sf.has_internet is not null as integer) + cast(sf.has_computer is not null as integer), 0),
          1) as decimal(4, 1)) as infra_index,
    sf.year_key
from fact_school_facilities sf
join dim_year y        on y.year_key = sf.year_key
join dim_school s      on s.school_key = sf.school_key
join dim_district d    on d.district_key = s.district_key
join dim_management m  on m.management_key = s.management_key
left join enrolment e  on e.school_key = sf.school_key and e.year_key = sf.year_key
left join fact_teachers t on t.school_key = sf.school_key and t.year_key = sf.year_key
