# DAX measures: Rajasthan UDISE+ Power BI model

Every measure the dashboard uses, with a one-line explanation. Table and column names are
those of the CSVs in `powerbi/data/` (see [BUILD_GUIDE.md](BUILD_GUIDE.md) for loading them).

**How to check them.** The same logic is written in SQL in `sql/powerbi/check_*.sql` and run on
the exported CSVs by `scripts/make_reconciliation_workbook.py`. The expected value of every
measure is in [`reconciliation.xlsx`](reconciliation.xlsx) (sheets *State measures* and *District
measures*). A card in Power BI with only the year filtered must show the same number. Example
values from the current run (2024-25) are given after some measures below.

Two definitions run through the whole model:

- **Validation measures** (section 1) use the **official definitions**: all schools, and enrolment
  including pre-primary. They are built on the original fact tables.
- **District indicators** (sections 2-5) count **only schools with students in classes 1-12**,
  the same as `mart_district_year`. They are built on `agg_school_year`, one row per school and year.

Put all measures in one empty table called `_Measures` (BUILD_GUIDE step 5), with the display
folder given in each section heading.

---

## 1. Validation (display folder `1 Validation`)

```dax
Schools = COUNTROWS ( fact_school_facilities )
```
Counts school-years: `fact_school_facilities` has exactly one row per school and year (2024-25: 1,06,302).

```dax
Enrolment (incl. pre-primary) = SUM ( fact_enrolment[students] )
```
Adds up every enrolment cell (grade x gender x social category); zero cells are not stored, so nothing is lost (2024-25: 1,63,64,187).

```dax
Enrolment Classes 1-12 = CALCULATE ( [Enrolment (incl. pre-primary)], dim_grade[grade_key] >= 1 )
```
The same total without pre-primary (grade_key 0); this is the enrolment the indicators use.

```dax
Teachers = SUM ( fact_teachers[total_tch] )
```
All teachers as reported by schools (2024-25: 7,92,265).

```dax
Single-Teacher Schools = CALCULATE ( COUNTROWS ( fact_teachers ), fact_teachers[total_tch] = 1 )
```
School-years with exactly one teacher (2024-25: 6,117).

```dax
Zero-Enrolment Schools =
[Schools]
    - COUNTROWS ( SUMMARIZE ( fact_enrolment, fact_enrolment[school_key], fact_enrolment[year_key] ) )
```
All schools minus schools with at least one enrolment row; works because zero cells are not stored (2024-25: 215).

```dax
Govt Schools = CALCULATE ( [Schools], dim_management[is_government] = TRUE () )
```
The filter on `dim_management` reaches the fact through `dim_school` (2024-25: 70,155).

```dax
Govt Enrolment = CALCULATE ( [Enrolment (incl. pre-primary)], dim_management[is_government] = TRUE () )
```
Enrolment in government schools, including pre-primary as published (2024-25: 77,82,390).

```dax
Govt Teachers = CALCULATE ( [Teachers], dim_management[is_government] = TRUE () )
```
Teachers in government schools (2024-25: 4,45,073).

```dax
Official Value = SUM ( official_totals[official_value] )
```
The published figure on the current row of the validation table (empty when nothing is published).

```dax
Pipeline Value =
VAR yr  = SELECTEDVALUE ( official_totals[year] )
VAR ind = SELECTEDVALUE ( official_totals[indicator] )
RETURN
    CALCULATE (
        SWITCH (
            ind,
            "schools",                [Schools],
            "enrolment",              [Enrolment (incl. pre-primary)],
            "enrolment_classes_1_12", [Enrolment Classes 1-12],
            "teachers",               [Teachers],
            "single_teacher_schools", [Single-Teacher Schools],
            "zero_enrolment_schools", [Zero-Enrolment Schools],
            "govt_schools",           [Govt Schools],
            "govt_enrolment",         [Govt Enrolment],
            "govt_teachers",          [Govt Teachers]
        ),
        TREATAS ( { yr }, dim_year[academic_year] )
    )
```
Picks the measure named on the row and computes it for that row's year: `official_totals` has no relationship, so `TREATAS` applies its year to `dim_year`.

```dax
Difference = IF ( NOT ISBLANK ( [Official Value] ), [Pipeline Value] - [Official Value] )
```
Pipeline minus official; empty when there is no official figure.

```dax
Difference % = DIVIDE ( [Difference], [Official Value] )
```
The difference as a share of the official figure.

```dax
Match Status =
SWITCH (
    TRUE (),
    ISBLANK ( [Official Value] ), "no official figure",
    [Difference] = 0, "exact match",
    "differs"
)
```
A text label for each row of the validation table.

```dax
Match Colour = SWITCH ( [Match Status], "exact match", "#1E7B34", "differs", "#C00000", "#7F7F7F" )
```
Green / red / grey for the status, used as "Field value" conditional formatting of the font colour.

```dax
Published Figures = CALCULATE ( COUNTROWS ( official_totals ), NOT ISBLANK ( official_totals[official_value] ) )
```
How many official figures there are to check against (currently 8, all for 2024-25).

```dax
Exact Matches =
COUNTROWS (
    FILTER (
        official_totals,
        NOT ISBLANK ( official_totals[official_value] )
            && [Pipeline Value] = official_totals[official_value]
    )
)
```
Rows where the pipeline equals the official figure; `[Pipeline Value]` inside `FILTER` is computed for each row.

```dax
Validation Summary = FORMAT ( [Exact Matches], "0" ) & " of " & FORMAT ( [Published Figures], "0" ) & " published figures match exactly"
```
The headline sentence for the validation page (currently "8 of 8 published figures match exactly").

---

## 2. District indicators (display folder `2 District indicators`)

```dax
Schools with Students = CALCULATE ( COUNTROWS ( agg_school_year ), agg_school_year[enr_1_12] > 0 )
```
Schools with at least one student in classes 1-12, the base of every district indicator (2024-25: 1,05,973).

```dax
Enrolment 1-12 = SUM ( agg_school_year[enr_1_12] )
```
Students in classes 1-12 (schools without such students add 0) (2024-25: 1,53,40,387).

```dax
Teachers (schools with students) = CALCULATE ( SUM ( agg_school_year[teachers] ), agg_school_year[enr_1_12] > 0 )
```
Teachers in those schools only, so the PTR is not diluted by teachers in empty schools (2024-25: 7,91,610).

```dax
PTR = DIVIDE ( [Enrolment 1-12], [Teachers (schools with students)] )
```
Pupil-teacher ratio: students per teacher, from totals rather than an average of school ratios (2024-25: 19.38).

```dax
Single-Teacher Schools % =
DIVIDE (
    CALCULATE ( SUM ( agg_school_year[single_teacher] ), agg_school_year[enr_1_12] > 0 ),
    [Schools with Students]
)
```
Share of schools run by one teacher (2024-25: 5.7%).

```dax
Infrastructure Index = CALCULATE ( AVERAGE ( agg_school_year[infra_index] ), agg_school_year[enr_1_12] > 0 )
```
Average of the 0-100 school index (8 basic facilities; playground left out, DATA_QUALITY_LOG #17) (2024-25: 79.37).

```dax
Girls' Share 1-12 = DIVIDE ( SUM ( agg_school_year[enr_girls_1_12] ), [Enrolment 1-12] )
```
Girls as a share of students in classes 1-12 (2024-25: 47.3%).

```dax
Girls' Retention Gap =
DIVIDE ( SUM ( agg_school_year[enr_secondary_plus_g] ), SUM ( agg_school_year[enr_secondary_plus] ) )
    - DIVIDE ( SUM ( agg_school_year[enr_primary_g] ), SUM ( agg_school_year[enr_primary] ) )
```
Girls' share in classes 9-12 minus their share in classes 1-5; negative means girls drop out more after primary.

```dax
% Schools with Electricity = CALCULATE ( AVERAGE ( agg_school_year[has_electricity] ), agg_school_year[enr_1_12] > 0 )
```
Share of schools with electricity; `has_*` is 1/0 and blank when not reported, and `AVERAGE` skips blanks (2024-25: 91.4%).

```dax
% Schools with Safe Water = CALCULATE ( AVERAGE ( agg_school_year[has_safe_water] ), agg_school_year[enr_1_12] > 0 )
```
Same pattern for a functional safe drinking-water source (2024-25: 98.5%).

```dax
% Schools with Girls' Toilet = CALCULATE ( AVERAGE ( agg_school_year[has_girls_toilet] ), agg_school_year[enr_1_12] > 0 )
```
Same pattern for a functional girls' toilet (2024-25: 87.3%). Copy it for any other `has_*` column.

---

## 3. Year-over-year change (display folder `3 Year over year`)

The pattern: find the selected year, then compute the same measure for the year before by
replacing the year filter. It needs exactly one year selected (a year slicer); otherwise it
returns blank. `dim_year` has two years, so the YoY measures show for 2025-26 only.

Note on districts: each year a school counts in the district it belonged to *that year*
(`dim_school` is SCD Type 2). The 180 schools moved from ALWAR to KOTPUTLI-BEHROR in 2025-26
therefore show up as a fall in Alwar and a rise in Kotputli-Behror.

```dax
Enrolment 1-12 PY =
VAR y = SELECTEDVALUE ( dim_year[start_year] )
RETURN
    IF ( NOT ISBLANK ( y ),
        CALCULATE ( [Enrolment 1-12], REMOVEFILTERS ( dim_year ), dim_year[start_year] = y - 1 ) )
```
Enrolment in the previous academic year; `REMOVEFILTERS` clears the slicer's year before setting the earlier one.

```dax
Enrolment 1-12 YoY % = DIVIDE ( [Enrolment 1-12] - [Enrolment 1-12 PY], [Enrolment 1-12 PY] )
```
Percentage change from last year; blank when there is no previous year (2025-26: -2.5%).

```dax
Teachers PY =
VAR y = SELECTEDVALUE ( dim_year[start_year] )
RETURN
    IF ( NOT ISBLANK ( y ),
        CALCULATE ( [Teachers (schools with students)], REMOVEFILTERS ( dim_year ), dim_year[start_year] = y - 1 ) )
```
Teachers in schools with students, previous year.

```dax
Teachers YoY % = DIVIDE ( [Teachers (schools with students)] - [Teachers PY], [Teachers PY] )
```
Percentage change in teachers (2025-26: +0.1%).

```dax
PTR PY =
VAR y = SELECTEDVALUE ( dim_year[start_year] )
RETURN IF ( NOT ISBLANK ( y ), CALCULATE ( [PTR], REMOVEFILTERS ( dim_year ), dim_year[start_year] = y - 1 ) )
```
Pupil-teacher ratio in the previous year.

```dax
PTR YoY Change = IF ( NOT ISBLANK ( [PTR PY] ), [PTR] - [PTR PY] )
```
Change in students per teacher; for a ratio the difference is clearer than a percentage (2025-26: -0.51).

```dax
Single-Teacher Schools % PY =
VAR y = SELECTEDVALUE ( dim_year[start_year] )
RETURN
    IF ( NOT ISBLANK ( y ),
        CALCULATE ( [Single-Teacher Schools %], REMOVEFILTERS ( dim_year ), dim_year[start_year] = y - 1 ) )
```
Share of single-teacher schools in the previous year.

```dax
Single-Teacher Schools % YoY Change = IF ( NOT ISBLANK ( [Single-Teacher Schools % PY] ), [Single-Teacher Schools %] - [Single-Teacher Schools % PY] )
```
Change in percentage points (format as `+0.0%`) (2025-26: +1.0 points).

```dax
Teacher Shortfall PY =
VAR y = SELECTEDVALUE ( dim_year[start_year] )
RETURN
    IF ( NOT ISBLANK ( y ),
        CALCULATE ( [Teacher Shortfall], REMOVEFILTERS ( dim_year ), dim_year[start_year] = y - 1 ) )
```
Teacher shortfall (section 5) in the previous year.

```dax
Teacher Shortfall YoY Change = IF ( NOT ISBLANK ( [Teacher Shortfall PY] ), [Teacher Shortfall] - [Teacher Shortfall PY] )
```
Change in the number of missing teachers (2025-26: -81).

---

## 4. Variance vs state average (display folder `4 vs State`)

"State" here is the value for **all of Rajasthan pooled**: all students over all teachers,
not the mean of the 41 district values (which would weight Jaisalmer the same as Jaipur).
`REMOVEFILTERS ( dim_district )` drops the district from the filter and keeps everything else
(year, management, rural/urban), so the comparison is like for like.

```dax
PTR (State) = CALCULATE ( [PTR], REMOVEFILTERS ( dim_district ) )
```
The state PTR for the same year and filters.

```dax
PTR vs State = [PTR] - [PTR (State)]
```
Positive = more students per teacher than the state (worse).

```dax
PTR vs State % = DIVIDE ( [PTR vs State], [PTR (State)] )
```
The same gap as a share of the state value.

```dax
Single-Teacher Schools % (State) = CALCULATE ( [Single-Teacher Schools %], REMOVEFILTERS ( dim_district ) )
```
The state share of single-teacher schools.

```dax
Single-Teacher Schools % vs State = [Single-Teacher Schools %] - [Single-Teacher Schools % (State)]
```
Difference in percentage points; positive = more single-teacher schools than the state.

```dax
Infrastructure Index (State) = CALCULATE ( [Infrastructure Index], REMOVEFILTERS ( dim_district ) )
```
The state infrastructure index.

```dax
Infrastructure Index vs State = [Infrastructure Index] - [Infrastructure Index (State)]
```
Points above (+) or below (-) the state.

```dax
Girls' Share 1-12 (State) = CALCULATE ( [Girls' Share 1-12], REMOVEFILTERS ( dim_district ) )
```
The state share of girls.

```dax
Girls' Share 1-12 vs State = [Girls' Share 1-12] - [Girls' Share 1-12 (State)]
```
Difference in percentage points.

```dax
Teacher Shortfall % (State) = CALCULATE ( [Teacher Shortfall %], REMOVEFILTERS ( dim_district ) )
```
The state teacher shortfall as a share of teachers required.

```dax
Teacher Shortfall % vs State = [Teacher Shortfall %] - [Teacher Shortfall % (State)]
```
Positive = a bigger share of required teachers missing than in the state.

```dax
PTR Above State = IF ( [PTR] > [PTR (State)], 1, 0 )
```
1/0 flag for conditional formatting and filtering ("districts worse than the state").

```dax
PTR (Mean of Districts) = AVERAGEX ( ALL ( dim_district[district_name] ), [PTR] )
```
For comparison only: the unweighted mean of the 41 district PTRs, to show why the pooled state value is used.

---

## 5. Resource gaps vs norms (display folder `5 Resource gaps`)

The norms are in `norms.csv` (from `reference/norms.json`), so they can be changed without
touching DAX:

| Stage | Classes | Pupils per teacher | Minimum teachers | Source |
|---|---|---:|---:|---|
| primary | 1-5 | 30 | 2 | RTE Act 2009, Schedule, item 1(a) (statutory, simplified) |
| upper_primary | 6-8 | 35 | 3 | RTE Act 2009, Schedule, item 1(b) (statutory) |
| secondary | 9-10 | 30 | 0 | RMSA framework (programme norm) |
| higher_secondary | 11-12 | 30 | 0 | no statutory norm: assumption |

Classrooms: **one classroom per teacher** (RTE Act 2009, Schedule, item 2).
Simplifications: head-teacher posts and the RTE rule of 40 pupils per teacher for primary
sections above 200 children are not modelled.

The gap is computed **school by school** and then added up: a spare teacher in one school
does not fill a vacancy in another. That is why the shortfall and the surplus can both be
large at the same time; the net gap is exactly their difference.

### Two calculated columns on `agg_school_year`

These are columns, not measures (Table tools > New column). They are computed once per school-year at refresh.

```dax
teachers_required =
VAR p_ppt = LOOKUPVALUE ( norms[pupils_per_teacher], norms[stage], "primary" )
VAR p_min = LOOKUPVALUE ( norms[min_teachers], norms[stage], "primary" )
VAR u_ppt = LOOKUPVALUE ( norms[pupils_per_teacher], norms[stage], "upper_primary" )
VAR u_min = LOOKUPVALUE ( norms[min_teachers], norms[stage], "upper_primary" )
VAR s_ppt = LOOKUPVALUE ( norms[pupils_per_teacher], norms[stage], "secondary" )
VAR s_min = LOOKUPVALUE ( norms[min_teachers], norms[stage], "secondary" )
VAR h_ppt = LOOKUPVALUE ( norms[pupils_per_teacher], norms[stage], "higher_secondary" )
VAR h_min = LOOKUPVALUE ( norms[min_teachers], norms[stage], "higher_secondary" )
VAR p = agg_school_year[enr_primary]
VAR u = agg_school_year[enr_upper_primary]
VAR s = agg_school_year[enr_secondary]
VAR h = agg_school_year[enr_higher_secondary]
RETURN
    IF ( p > 0, MAX ( p_min, ROUNDUP ( p / p_ppt, 0 ) ), 0 )
        + IF ( u > 0, MAX ( u_min, ROUNDUP ( u / u_ppt, 0 ) ), 0 )
        + IF ( s > 0, MAX ( s_min, ROUNDUP ( s / s_ppt, 0 ) ), 0 )
        + IF ( h > 0, MAX ( h_min, ROUNDUP ( h / h_ppt, 0 ) ), 0 )
```
Teachers this school needs: for each stage that has students, the larger of the minimum and students / norm rounded up.

```dax
teachers_for_norms = agg_school_year[teachers] - agg_school_year[teachers_pre_primary_only]
```
Teachers who can count against the norms: those teaching only pre-primary are left out (8,386 of 7,92,265 in 2024-25).

### Measures

```dax
Teachers Required (Norm) = CALCULATE ( SUM ( agg_school_year[teachers_required] ), agg_school_year[enr_1_12] > 0 )
```
Total teachers needed under the norms (2024-25: 7,11,638).

```dax
Teacher Shortfall =
SUMX (
    FILTER ( agg_school_year, agg_school_year[enr_1_12] > 0 ),
    MAX ( 0, agg_school_year[teachers_required] - agg_school_year[teachers_for_norms] )
)
```
Teachers missing, added up over schools that have fewer than they need (2024-25: 78,356).

```dax
Teacher Surplus =
SUMX (
    FILTER ( agg_school_year, agg_school_year[enr_1_12] > 0 ),
    MAX ( 0, agg_school_year[teachers_for_norms] - agg_school_year[teachers_required] )
)
```
Teachers above the norm, added up over schools that have more than they need (2024-25: 1,49,978).

```dax
Net Teacher Gap =
[Teachers Required (Norm)]
    - CALCULATE ( SUM ( agg_school_year[teachers_for_norms] ), agg_school_year[enr_1_12] > 0 )
```
Required minus available overall (= shortfall - surplus); negative means enough teachers in total, so the problem is where they are posted (2024-25: -71,622).

```dax
Teacher Shortfall % = DIVIDE ( [Teacher Shortfall], [Teachers Required (Norm)] )
```
The shortfall as a share of required teachers, to compare districts of different size (2024-25: 11.0%).

```dax
Schools Short of Teachers =
COUNTROWS (
    FILTER (
        agg_school_year,
        agg_school_year[enr_1_12] > 0
            && agg_school_year[teachers_for_norms] < agg_school_year[teachers_required]
    )
)
```
Number of schools below the norm (2024-25: 26,651).

```dax
Classroom Shortfall =
SUMX (
    FILTER ( agg_school_year, agg_school_year[enr_1_12] > 0 ),
    MAX ( 0, agg_school_year[teachers] - agg_school_year[classrooms] )
)
```
Classrooms missing for one room per teacher, counting every classroom (2024-25: 1,32,135).

```dax
Classroom Shortfall (Usable Rooms) =
SUMX (
    FILTER ( agg_school_year, agg_school_year[enr_1_12] > 0 ),
    MAX ( 0, agg_school_year[teachers] - agg_school_year[classrooms_usable] )
)
```
The same counting only rooms in good condition or needing minor repair (2024-25: 1,81,116).

---

Source: UDISE+, Ministry of Education, Government of India.
