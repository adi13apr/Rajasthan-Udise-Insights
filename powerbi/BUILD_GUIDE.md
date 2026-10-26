# Power BI build guide: Rajasthan UDISE+ dashboard

Step-by-step instructions to build the dashboard in **Power BI Desktop** (free, Windows) from
the gold star schema: load the CSVs, set the relationships, add the measures from
[MEASURES.md](MEASURES.md), and build three pages: **State overview**, **District comparison**
and **Validation**.

Check each result against [`reconciliation.xlsx`](reconciliation.xlsx). It holds the expected
value of every measure, computed independently from the same CSVs.

## Quick path: open the generated project

The finished dashboard is generated as a **Power BI project** (`.pbip`), Power BI Desktop's
text format: the model in TMDL files and the report in JSON files, reviewable in git. It holds
no data.

```powershell
python scripts/export_powerbi.py          # gold -> powerbi/data/*.csv
python scripts/make_powerbi_project.py    # -> powerbi/RajasthanUDISE.pbip (+ .SemanticModel, .Report)
```

Open `powerbi/RajasthanUDISE.pbip` in Power BI Desktop. The project holds no data, so the first
time Desktop shows empty visuals and a yellow bar *Some of the tables have incomplete or no data*:
click **Refresh now** (or *Home > Refresh*). On the development laptop the full load took 145 s;
the 64 lakh enrolment rows take the longest. The CSV folder is
the `DataFolder` parameter (*Transform data > Edit parameters*). It is set to this checkout's
`powerbi/data/` when the project is generated.

Everything in sections 1-8 below is already in the project: types, sort orders, the 13
relationships, the calculated columns, all measures from MEASURES.md (read from that file by the
generator, so the two cannot drift apart), and the three pages. The sections explain how the
project is built, so you can defend every part of it or build it again by hand.

**Check the model** while it is open in Desktop:

```powershell
python scripts/verify_powerbi_model.py              # after the data is loaded
python scripts/verify_powerbi_model.py --refresh    # or: let the script load it first
```

This runs DAX queries against Desktop's local engine and compares every value with the SQL
re-implementation. The result is in [`model_check.md`](model_check.md). It talks to the engine
through ADOMD.NET, Microsoft's client library, using the copy that ships with Power BI Desktop.

To share the design, commit the `.pbip` project. Never commit a `.pbix` and never use
*Publish to web*: both carry the school-level data.

---

## 0. Before you start

```powershell
python run_pipeline.py                          # builds data/gold/udise.duckdb (skip if it exists)
python scripts/export_powerbi.py                # gold -> powerbi/data/*.csv
python scripts/make_reconciliation_workbook.py  # powerbi/reconciliation.xlsx + checks
```

If the gold database lives in another folder (for example in another git worktree), add
`--gold-db <path to udise.duckdb>` to both scripts.

What `powerbi/data/` then holds (rows from `export_manifest.json`, current run):

| File | Rows | What it is |
|---|---:|---|
| `dim_year.csv` | 2 | academic years 2024-25, 2025-26 |
| `dim_district.csv` | 41 | districts (with the old spellings in `aliases`) |
| `dim_management.csv` | 14 | management codes and groups; `is_government` |
| `dim_grade.csv` | 13 | pre-primary and classes 1-12, with stage |
| `dim_gender.csv` | 3 | boys, girls, transgender (2025-26 only) |
| `dim_social_category.csv` | 4 | General, SC, ST, OBC |
| `dim_school.csv` | 1,09,037 | one row per *version* of a school (SCD Type 2); no school code |
| `fact_enrolment.csv` | 64,03,516 | students per school x year x grade x gender x category |
| `fact_teachers.csv` | 2,12,747 | teachers per school and year |
| `fact_school_facilities.csv` | 2,12,747 | facilities and classrooms per school and year |
| `agg_school_year.csv` | 2,12,747 | helper: enrolment by stage, teachers, classrooms per school and year |
| `official_totals.csv` | 18 | published figures to validate against (empty where none) |
| `norms.csv` | 4 | teacher norms per stage (RTE Act / RMSA) |

> **Data-sharing terms.** The facts, `dim_school` and `agg_school_year` are school-level. They
> are gitignored, and so is any `.pbix` you save in `powerbi/`, because a .pbix contains the data.
> Never use *Publish to web* with this model. To share the design, export a template instead
> (*File > Export > Power BI template*, `.pbit`), which holds no data.
> On every page put: *Source: UDISE+, Ministry of Education, Government of India.*

---

## 1. Settings (once per file)

*File > Options and settings > Options*:

1. **Current File > Data Load:** untick *Autodetect new relationships after data is loaded*.
   You will create the relationships yourself (step 3), so you know exactly why each one exists.
2. **Current File > Data Load:** untick *Auto date/time*. The model has no dates; this avoids hidden tables.

---

## 2. Load the CSVs

For each of the 13 CSV files:

1. *Home > Get data > Text/CSV*, pick the file.
2. In the preview set **File origin** = *65001: Unicode (UTF-8)*, **Delimiter** = *Comma* and
   **Data type detection** = *Based on entire dataset*. The default looks at the first 200 rows
   only, and can then guess a column's type wrongly.
3. Click **Load**. Power BI names the table after the file (`fact_enrolment`, ...), which is the
   name the measures use.

After loading, check these types in the *Data* view (*Column tools > Data type*) and fix any that differ:

| Column(s) | Type |
|---|---|
| every `*_key` column, `students`, `total_tch`, `enr_*`, `teachers*`, `classrooms*`, `official_value`, `pupils_per_teacher`, `min_teachers` | Whole number |
| `dim_school[is_current]`, `dim_management[is_government]` | True/False |
| `agg_school_year[infra_index]`, every `has_*` column, `pct_classrooms_major_repair` | Decimal number |
| `dim_year[academic_year]`, `official_totals[year]`, `dim_grade[grade_code]` | Text |

`is_government` must be True/False, or the government measures return blank.

Sort orders (*Data* view, select the column, *Column tools > Sort by column*):

- `dim_year[academic_year]` sorted by `dim_year[start_year]`
- `dim_grade[grade_label]` sorted by `dim_grade[grade_key]` (otherwise "Class 10" comes before "Class 2")

---

## 3. Relationships (star schema)

*Model* view > *Manage relationships > New*. Create these 11 relationships, all **many-to-one
(\*:1)** and **single** cross-filter direction (from the dimension to the fact):

| From (many side) | To (one side) |
|---|---|
| `fact_enrolment[school_key]` | `dim_school[school_key]` |
| `fact_enrolment[year_key]` | `dim_year[year_key]` |
| `fact_enrolment[grade_key]` | `dim_grade[grade_key]` |
| `fact_enrolment[gender_key]` | `dim_gender[gender_key]` |
| `fact_enrolment[social_category_key]` | `dim_social_category[social_category_key]` |
| `fact_teachers[school_key]` | `dim_school[school_key]` |
| `fact_teachers[year_key]` | `dim_year[year_key]` |
| `fact_school_facilities[school_key]` | `dim_school[school_key]` |
| `fact_school_facilities[year_key]` | `dim_year[year_key]` |
| `agg_school_year[school_key]` | `dim_school[school_key]` |
| `agg_school_year[year_key]` | `dim_year[year_key]` |

And two from `dim_school` to its outer dimensions (a *snowflake*, as in gold):

| From (many side) | To (one side) |
|---|---|
| `dim_school[district_key]` | `dim_district[district_key]` |
| `dim_school[management_key]` | `dim_management[management_key]` |

That is 13 in total. `official_totals` and `norms` get **no relationship**: the measures read them
directly (`TREATAS`, `LOOKUPVALUE`).

How to explain the model:

- **Why single direction:** filters flow from dimensions to facts only. A district slicer filters
  `dim_district` > `dim_school` > every fact. Two-way filters between facts would make results
  depend on which visual is filtering and slow the model down.
- **Why `dim_school` is keyed on the version:** a school that moved district has two rows
  (SCD Type 2). Each fact row points to the version valid in its year, so a 2024-25 row of a
  school moved from ALWAR counts in Alwar, and its 2025-26 row in Kotputli-Behror.
- **Why `agg_school_year`:** the resource-gap measures compare enrolment, teachers and
  classrooms *school by school*. That table has all three on one row per school-year (2,12,747
  rows), so the measures are a simple `SUMX`. Without it, every school would need a lookup into
  the 64 lakh enrolment rows.

Tidy up: right-click every `*_key` column in the fact tables and `agg_school_year`, then
*Hide in report view*. Report users pick districts and years from the dimensions, never from keys.

---

## 4. Calculated columns

Select `agg_school_year`, then *Table tools > New column*. Paste the two column definitions from
MEASURES.md section 5: `teachers_required` and `teachers_for_norms`.

Worked example of `teachers_required`: a school with 61 primary and 36 upper-primary students
needs max(2, roundup(61 / 30)) = 3 primary teachers plus max(3, roundup(36 / 35)) = 3
upper-primary teachers, so 6. The same case is a unit test (`tests/test_powerbi.py`).

---

## 5. Measures

1. *Home > Enter data*, name the table `_Measures`, *Load*. This gives an empty table to hold
   all measures in one place.
2. Select `_Measures`, then *Home > New measure*. Paste each measure from MEASURES.md, one at a
   time, in the order given: later measures use earlier ones.
3. Delete the dummy column `Column1` from `_Measures`. The table then shows a calculator icon
   and moves to the top of the field list.
4. In the *Model* view select each measure and set its **Display folder** (Properties pane) as
   given in the MEASURES.md section headings, and its **format**:
   - counts (`Schools`, `Teachers`, `Teacher Shortfall`, ...): Whole number, thousands separator
   - `PTR`, `Infrastructure Index` and their `vs State` measures: Decimal, 1 place
   - shares and the `%` measures, `Girls' Retention Gap`, `Difference %`: Percentage, 1 place
     (`Difference %`: 3 places)
5. **Check:** put a card with `Teacher Shortfall` on an empty page and a slicer on
   `dim_year[academic_year]`. 2024-25 must show **78,356** and 2025-26 **78,275**. Compare a few
   more measures with the *State measures* sheet of `reconciliation.xlsx`. If a value differs,
   check the data types (step 2) and the relationships (step 3) first.

---

## 6. Page 1: State overview

Purpose: the headline numbers for Rajasthan in one year and how they changed.

1. **Year slicer:** Slicer with `dim_year[academic_year]`, style *Tile*, *Single select* on.
   Copy it to the other pages later (*View > Sync slicers*: tick pages 1 and 2).
2. **KPI cards** (visual *Card (new)*), one row across the top:
   - `Schools`, `Enrolment (incl. pre-primary)`, `Teachers`: the official-definition totals
   - `PTR`, with reference label `PTR YoY Change`
   - `Single-Teacher Schools %`, with reference label `Single-Teacher Schools % YoY Change`
   - `Teacher Shortfall`, with reference label `Teacher Shortfall YoY Change`
   - `Classroom Shortfall`

   With 2024-25 selected the YoY labels are blank (there is no earlier year in the data).
3. **Enrolment by stage and gender:** *Clustered bar chart*. Y-axis `dim_grade[stage]`,
   X-axis `Enrolment (incl. pre-primary)`, Legend `dim_gender[gender_label]`. Note on the page:
   transgender students are reported separately only from 2025-26; in 2024-25 they are included
   with boys.
4. **Schools and teachers by management:** *Clustered bar chart*. Y-axis
   `dim_management[management_group]`, X-axis `Schools` and `Teachers`. Sort by `Schools`.
5. **Facilities:** *Clustered bar chart* with the three measures `% Schools with Electricity`,
   `% Schools with Safe Water` and `% Schools with Girls' Toilet` on the X-axis and no category
   (one bar per measure). Optional: a card with `Infrastructure Index`.
6. **Resource gap:** *Clustered column chart*, X-axis `dim_year[academic_year]`, Y-axis
   `Teacher Shortfall` and `Teacher Surplus`. This shows the main finding: there are more
   teachers above the norm than missing (net gap 2024-25: -71,622), so the problem is posting,
   not numbers.
7. Text box at the bottom: *Source: UDISE+, Ministry of Education, Government of India.
   District indicators count schools with students in classes 1-12. Norms: RTE Act 2009
   (classes 1-8), RMSA (classes 9-10), 30:1 assumed for 11-12.*

---

## 7. Page 2: District comparison

Purpose: rank the 41 districts and show how far each is from the state.

1. **Year slicer** (synced from page 1).
2. **District table:** *Table* visual with `dim_district[district_name]` and the measures
   `Enrolment 1-12`, `PTR`, `PTR vs State`, `Single-Teacher Schools %`,
   `Single-Teacher Schools % vs State`, `Infrastructure Index`, `Infrastructure Index vs State`,
   `Teacher Shortfall`, `Teacher Shortfall %`, `Teacher Shortfall % vs State`, `Classroom Shortfall`.
   - Conditional formatting on each `vs State` column: *Cell elements > Background color > fx*,
     *Format style* = Gradient, tick *Add a middle color*, Minimum / Center / Maximum =
     Lowest value / Number 0 / Highest value. This gives a diverging scale centred on the state.
     For `PTR vs State`, `Single-Teacher Schools % vs State` and `Teacher Shortfall % vs State`,
     high is bad (red at the maximum). For `Infrastructure Index vs State`, high is good (reverse
     the colours).
   - Turn **Totals** on: the total row then shows the state values, and every `vs State` total is 0.
3. **PTR by district with the state line:** *Line and clustered column chart*. X-axis
   `dim_district[district_name]`, Column y-axis `PTR`, Line y-axis `PTR (State)`. Sort by `PTR`
   descending. Districts whose bar crosses the line are above the state ratio.
4. **Teacher shortfall vs surplus by district:** *Clustered bar chart*, Y-axis
   `dim_district[district_name]`, X-axis `Teacher Shortfall` and `Teacher Surplus`, sorted by
   `Teacher Shortfall`. A district with both bars long has enough teachers but in the wrong schools.
5. **Optional filters:** slicers on `dim_management[management_group]` and `dim_school[location]`
   (Rural/Urban). The `vs State` measures follow them, because they remove only the district filter.
6. Note on the page: *District = the district a school belonged to in that year. In 2025-26,
   180 schools moved from Alwar to the new Kotputli-Behror district, so year-on-year changes for
   these two districts include the move.*
7. **Check:** pick 2024-25. Ajmer's `PTR vs State` must be **-0.33**. All 82 district-year values
   are in the *District measures* sheet of `reconciliation.xlsx`.

Why no map: several districts were created in 2023 (Balotra, Kotputli-Behror, ...). Bing's
geocoding of district names is unreliable for them. A filled map needs a shape file with the
current boundaries (*Shape map* visual with a TopoJSON of Rajasthan's 41 districts). Add one only
if you have such a file from an official source.

---

## 8. Page 3: Validation (pipeline totals vs official figures)

Purpose: prove the numbers on the other pages reproduce the published UDISE+ figures.

1. **Year slicer** on `official_totals[year]`, a different field from pages 1-2: this table has
   no relationship to `dim_year`. Do not sync it.
2. **Headline card:** `Validation Summary`. It must read *8 of 8 published figures match exactly*.
3. **Validation table:** *Table* with `official_totals[year]`, `official_totals[indicator_label]`,
   `Official Value`, `Pipeline Value`, `Difference`, `Difference %`, `Match Status`.
   - Conditional formatting on `Match Status`: *Font color > fx*, *Format style* = Field value,
     field = `Match Colour`.
   - Turn totals off (a total of different indicators means nothing).
   - Expected: for 2024-25 every published row says *exact match* with difference 0. Rows for
     2025-26 and *Enrolment, classes 1-12* say *no official figure*.
4. **Text box** with the definitions and sources:
   *Official figures: UDISE+ 2024-25 report (Table 2.2) and Lok Sabha / Rajya Sabha answers for
   government schools. Enrolment includes pre-primary, as published. Government = management
   codes grouped as government in reference/management_codes.json. The same comparison, with a
   second independent calculation from the CSVs, is in powerbi/reconciliation.xlsx.*
5. **Check:** the values must match the *Reconciliation* sheet of `reconciliation.xlsx` row for row.

---

## 9. Refreshing after a new pipeline run

```powershell
python run_pipeline.py
python scripts/export_powerbi.py
python scripts/make_reconciliation_workbook.py   # must end with "14 of 14 checks passed"
```

Then in Power BI Desktop click *Home > Refresh*. The calculated columns are recomputed on
refresh. If you change `reference/norms.json`, re-run the export so that `norms.csv` changes,
then refresh.

---

Source: UDISE+, Ministry of Education, Government of India.
