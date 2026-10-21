# Building the Tableau Public dashboard

A step-by-step recipe for rebuilding the project's dashboard in Tableau Public (free). It mirrors
the web version (Overview, Girls in secondary, Teachers), uses only the committed district-level
files, and takes about an hour the first time.

## 1. Install and connect

1. Install Tableau Public from public.tableau.com (free; you sign in with your own account to save).
2. **Connect > Text file** and open `data/gold/exports/tableau_district_indicators.csv`
   (one row per district and year, 82 rows).
3. Add two more text-file connections (Data > New Data Source):
   - `reports/analysis/a4_single_teacher_change_matched.csv` (41 rows, same schools in both years)
   - `reports/analysis/a2_girls_share_by_sector_2025-26.csv` (82 rows, government vs non-government)
4. In the data pane, check the types: `year` and `district` are strings (Abc); every indicator is a
   number. Rename fields to plain labels as you go (right-click > Rename), e.g. `avg_infra_index`
   -> "Infrastructure index".

## 2. Parameters and calculated fields (main data source)

**Parameters** (right-click in the data pane > Create Parameter):
- `Year`: string, list `2024-25`, `2025-26`; current value `2025-26`. Show the parameter control.
- `Indicator`: string, list `Infrastructure index`, `Single-teacher schools (%)`,
  `Students per teacher`, `Girls' retention gap`. Show the parameter control.

**Calculated fields:**
```
Selected year         [year] = [Year]
Indicator value       CASE [Indicator]
                        WHEN "Infrastructure index" THEN [avg_infra_index]
                        WHEN "Single-teacher schools (%)" THEN [pct_single_teacher]
                        WHEN "Students per teacher" THEN [ptr]
                        WHEN "Girls' retention gap" THEN [girls_retention_gap] * 100
                      END
Worse is high         [Indicator] <> "Infrastructure index" AND [Indicator] <> "Girls' retention gap"
Rank (1 = worst)      RANK_UNIQUE(IF [Worse is high] THEN SUM([Indicator value]) ELSE -SUM([Indicator value]) END)
Worst 10              [Rank (1 = worst)] <= 10
District (title)      PROPER([district])
State PTR             SUM([enrolment]) / SUM([teachers])
State single-teacher  SUM([pct_single_teacher] * [schools]) / SUM([schools])
State infra index     SUM([avg_infra_index] * [schools]) / SUM([schools])
Retention gap (pts)   [girls_retention_gap] * 100
```
`Rank` and `Worst 10` are table calculations: set **Compute using > District (title)**.
`PROPER` turns "SAWAI MADHOPUR" into "Sawai Madhopur".

## 3. Worksheets

Put `Selected year` on Filters (= True) in every sheet except the two from the other files.

| Sheet | Build | Format |
|---|---|---|
| **KPI tiles** (6 sheets, one number each) | Text mark: `SUM(schools)`, `SUM(enrolment)`, `SUM(teachers)`, `State PTR`, `State single-teacher`, `State infra index` | Big number in a serif font; custom number format `#,##,##0` for Indian grouping; a title like "Schools with students" |
| **District ranking** | Rows: `District (title)`; Columns: `Indicator value`; sort by `Rank`. Colour: `Worst 10` (True = blue `#2a78d6`, False = grey `#d3d2cb`). Label: `Indicator value`, only for the worst 10 | Add a reference line at the state value (Analytics pane > Reference line, average weighted by `schools`); title: `<Parameters.Indicator>, <Parameters.Year>` |
| **District profile** | Text table filtered to one district (it is driven by the action in step 4): infrastructure index, single-teacher %, students per teacher, retention gap, girls' toilet %, students; plus `rank_avg_infra_index` etc. "of 41" | Put it in a narrow column beside the ranking |
| **Gender patterns** | Scatter: Columns `boys_progression`, Rows `girls_progression`, Detail `district`, Colour `pattern` (girls leaving `#eb6834`, boys moving in `#2a78d6`, boys leaving `#1baf7a`, other `#d3d2cb`). Label the coloured districts | Add a 45-degree reference line: a calculated field `[boys_progression]` on a dual axis, or annotate "equal for girls and boys" |
| **Government vs non-government** (sector file) | Dumbbell: Rows `district`; Columns `girls_retention_gap` (as points) and again as a line with `sector` on Path; Colour `sector` (Government blue, Non-government orange) | Sort by the non-government value; zero line |
| **Single-teacher change** (matched file) | Rows `district`, Columns `change_pp`; colour the four western desert districts (Jaisalmer, Barmer, Phalodi, Balotra) orange with a set | Reference line at the state change (+1.0); tooltip: 2024-25 % -> 2025-26 %, teachers change % |
| **Infrastructure vs single-teacher** | Scatter: Columns `pct_single_teacher`, Rows `avg_infra_index`, colour the tribal-south districts teal | Reference lines at the state averages, so the bottom-right quadrant is "worse on both" |

## 4. Dashboards

Make three dashboards (Size: Automatic, or fixed 1200 x 900) and join them as a **Story** or with
navigation buttons (Objects > Navigation):

1. **Overview**: title and one-line takeaway at the top; the six KPI tiles in a horizontal
   container; below, the ranking (about 60% width) and the district profile (40%). Add the `Year`
   and `Indicator` parameter controls in the header.
   **Action:** Dashboard > Actions > Add Action > Filter: source = ranking, target = profile, run
   on *Select*, clearing shows all values. Clicking a bar then shows that district's profile.
2. **Girls in secondary**: a text box with the three patterns (girls leaving: Jaisalmer, Barmer;
   boys moving in: Sikar, Kota, Jhunjhunu; boys leaving: the tribal south), then the scatter and the
   government vs non-government dumbbell side by side.
3. **Teachers**: the single-teacher change bars and the infrastructure vs single-teacher scatter.

Keep the source line on every dashboard: *Source: UDISE+, Ministry of Education, Government of
India. District aggregates; associations, not causes.*

## 5. Publish

File > Save to Tableau Public (signs you in). In the workbook's settings on public.tableau.com,
allow downloads if reviewers should explore the data, then put the link in the README and on your
resume. Only district-level aggregates are in these files, which the UDISE+ terms allow you to
publish.

## Design notes (the same rules as the web dashboard)

- One highlight colour per chart; everything else grey. Text in dark grey, never in a series colour.
- Indian number grouping (1,06,167) and units (lakh, crore) for counts.
- Each chart's title says what it shows and its year; each note says how the measure is defined.
- The patterns and the model are associations: say so on the dashboard.
