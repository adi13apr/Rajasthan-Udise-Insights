# Data Quality Log

Issues found while building the Rajasthan UDISE+ dataset, and what was done about
each. Automated checks are regenerated on every run in `reports/qa_report.md`;
this file records the findings and the decisions behind them.

**Source:** UDISE+ Data Sharing Portal (microdata.udiseplus.gov.in), Ministry of
Education, Government of India. School-level files for 2024-25 and 2025-26.
Schemas: `docs/DSP_Schema_2024-25.pdf`, `docs/DSP_Schema_2025-26.pdf`.

**Coverage:** all 41 districts, 1,06,302 schools (2024-25) and 1,06,445 (2025-26).

## How the data was assembled

| # | Issue | Evidence | Decision |
|---|---|---|---|
| 1 | No state-level enrolment file | The portal's "All districts" option works for profile, facility and teacher files, but returns "File Not Found" for Enrollment-1 in both years | Enrollment-1 downloaded for each of the 41 districts separately (`download_udise.py --by-district`); other files downloaded state-wide. The pipeline joins them on school code. |
| 2 | Same schools in two files | Kota was downloaded on its own for testing and again inside the state files (4,520 duplicate rows) | Pipeline keeps one copy of each school-year and reports how many duplicates it dropped. |
| 3 | District spelled differently across years | BALOTARA/BALOTRA, CHITTAURGARH/CHITTORGARH, DHAULPUR/DHOLPUR, JALOR/JALORE, PRATAPGARH (RAJ.)/PRATAPGARH | Mapped to one spelling (`DISTRICT_ALIASES` in `config.py`). Both years then have the same 41 districts. |

## Validation against published figures

| # | Check | Result |
|---|---|---|
| 4 | Total schools, 2024-25 | 1,06,302, an **exact match** with the UDISE+ 2024-25 report (Table 2.2). |
| 5 | Schools with no enrolment, 2024-25 | 215 schools have no enrolment rows, an **exact match** with the official zero-enrolment count. 114 more have enrolment rows that sum to zero for classes 1-12 (e.g. pre-primary only); all 329 are excluded from district summaries. |
| 6 | Enrolment, teachers, government-school totals | Compared automatically in `qa_report.md` ("Validation against official UDISE+ totals"). See that table for the latest differences. |

## Issues in the data itself

| # | Issue | Evidence | Decision |
|---|---|---|---|
| 7 | `total_class_rooms` is 0 for every school in 2024-25 | The field is empty state-wide in 2024-25, while classrooms are reported by condition (good / minor repair / major repair) | Classroom count rebuilt as the sum of the three condition columns. In 2025-26, wherever `total_class_rooms` is filled in, this sum matches it exactly. |
| 8 | Yes/No questions use a third code, `9` | e.g. `smc_exists` = 9 for private schools, which are not required to have an SMC | `9` treated as "not applicable" (missing), never as "No". |
| 9 | Central-government schools list an admin region as their block | In 2025-26, 114 schools (79 with management code 92, 35 with code 93) have block = "JAIPUR REGION" | Kept; `block_clean` left blank and flagged. District totals are unaffected. The counts fit Kendriya Vidyalayas and Navodaya Vidyalayas, which are run through regional offices (inferred, not confirmed). |
| 10 | Management codes not documented in the schema | Codes 1, 2, 3, 5, 6, 90, 92, 93, 94, 95, 97, 98, 101 appear | Provisional grouping in `config.py`. Being checked against the official count of government schools (70,155 in 2024-25) in `qa_report.md`. **To do:** confirm with the UDISE+ DCF. |
| 11 | Undocumented disability code | Enrolment item_group 4 contains item_id 21; the schema lists only 1-20 | Included in the CWSN total; flagged. |
| 12 | Schema changes between years | 2025-26 adds transgender enrolment columns, 12 facility columns and 11 profile columns | Years are stacked with all columns; missing columns left empty. Only indicators present in both years are used for comparisons. |
| 13 | Column name differs from schema | Data: `teacher_involve_non_training_assignment`; schema: `teachers_involve_...` | Kept the name used in the data. |
| 14 | State-wide drop in playgrounds | Playground availability fell more than 5 percentage points in 24 of 41 districts in a single year | Almost certainly a change in reporting, not real loss. Playground is excluded from the infrastructure index and from trend claims. |
| 15 | Unexplained toilet drop in one district | Jhalawar: functional girls' toilets down 5.6 points and boys' down 6.3 points in one year; no other district moved this much | Kept, flagged as needing verification before being reported as a finding. |
| 16 | Schools changing district between years | Alwar lost 178 schools while Kotputli-Behror gained 191 between 2024-25 and 2025-26 | Being checked in `qa_report.md` ("Schools that moved to a different district"). If confirmed, year-on-year district changes for these two districts reflect boundary changes, not real growth or decline. |
| 17 | Implausible staffing | Schools with students but no teachers (268 in 2024-25, 366 in 2025-26); pupil-teacher ratio above 60 (1,224 and 1,165 schools) | Kept and flagged (`flag_no_teachers_with_students`, `flag_high_ptr`), not silently dropped. |
| 18 | Schools in only one year | School codes are stable across years: 1,05,418 match; 884 appear only in 2024-25 and 1,027 only in 2025-26 | Treated as closures/mergers and new schools. School-level year-on-year comparisons use matched schools only. |
| 19 | Blank location names | LGD village fields are blank for urban schools and urban fields are blank for rural schools | Structural, not missing data. No action needed. |