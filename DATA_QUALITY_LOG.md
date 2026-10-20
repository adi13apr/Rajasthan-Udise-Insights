# Data Quality Log

Issues found while building the Rajasthan UDISE+ dataset, and what was done about
each. Automated checks are regenerated on every run in `reports/qa_report.md`;
this file records the findings and the decisions behind them.

**Source:** UDISE+ Data Sharing Portal (microdata.udiseplus.gov.in), Ministry of
Education, Government of India. School-level files for 2024-25 and 2025-26.
Schemas: `docs/DSP_Schema_2024-25.pdf`, `docs/DSP_Schema_2025-26.pdf`.

**Coverage:** all 41 districts; 1,06,302 schools in 2024-25 and 1,06,445 in 2025-26.

## How the data was assembled

| # | Issue | Evidence | Decision |
|---|---|---|---|
| 1 | No state-level enrolment file | The portal's "All districts" option works for profile, facility and teacher files, but returns "File Not Found" for Enrollment-1 in both years | Enrollment-1 downloaded for each of the 41 districts separately (`download_udise.py --by-district`); other files downloaded state-wide. The pipeline joins them on school code. |
| 2 | Same schools in two files | Kota was downloaded on its own for testing and again inside the state files (4,520 duplicate rows) | Pipeline keeps one copy of each school-year and reports how many duplicates it dropped. |
| 3 | District spelled differently across years | BALOTARA/BALOTRA, CHITTAURGARH/CHITTORGARH, DHAULPUR/DHOLPUR, JALOR/JALORE, PRATAPGARH (RAJ.)/PRATAPGARH | Mapped to one spelling (`DISTRICT_ALIASES` in `config.py`). Both years then have the same 41 districts. |

## Validation against published figures

All 2024-25 checks below are run automatically in `reports/qa_report.md`.

| # | Check | Official | Pipeline | Result |
|---|---|---|---|---|
| 4 | Schools | 1,06,302 | 1,06,302 | Exact match |
| 5 | Teachers | 7,92,265 | 7,92,265 | Exact match |
| 6 | Enrolment (incl. pre-primary) | 1,63,64,187 | 1,63,64,187 | Exact match. The official figure includes pre-primary; classes 1-12 alone total 1,53,40,387, which is what the analysis uses. |
| 7 | Single-teacher schools | 6,117 | 6,117 | Exact match |
| 8 | Zero-enrolment schools | 215 | 215 | Exact match once pre-primary is counted. 114 further schools have only pre-primary students; they are excluded from class 1-12 summaries. |
| 9 | Government schools / teachers | 70,155 / 4,45,073 | see report | Used to settle the management-code grouping (#13). |

## Issues in the data itself

| # | Issue | Evidence | Decision |
|---|---|---|---|
| 10 | `total_class_rooms` is 0 for every school in 2024-25 | The field is empty state-wide in 2024-25, while classrooms are reported by condition (good / minor repair / major repair) | Classroom count rebuilt as the sum of the three condition columns. In 2025-26, wherever `total_class_rooms` is filled in, this sum matches it exactly. |
| 11 | Yes/No questions use a third code, `9` | e.g. `smc_exists` = 9 for private schools, which are not required to have an SMC | `9` treated as "not applicable" (missing), never as "No". |
| 12 | Central-government schools list an admin region as their block | In 2025-26, 114 schools (79 with management code 92, 35 with code 93) have block = "JAIPUR REGION" | Kept; `block_clean` left blank and flagged. District totals are unaffected. The counts fit Kendriya Vidyalayas and Navodaya Vidyalayas, which are run through regional offices (inferred, not confirmed). |
| 13 | Management codes not documented in the schema | Codes 1, 2, 3, 5, 6, 90, 92, 93, 94, 95, 97, 98, 101 appear | Grouped using the official government totals: counting 94, 95 and 101 as government makes government teachers match the official 4,45,073 exactly (those 3 codes have exactly the 7 schools and 170 teachers that were otherwise missing). 97 and 98 are non-government. Exact names of these codes still to confirm with the UDISE+ DCF. |
| 14 | Undocumented disability code | Enrolment item_group 4 contains item_id 21; the schema lists only 1-20 | Included in the CWSN total; flagged. |
| 15 | Schema changes between years | 2025-26 adds transgender enrolment columns, 12 facility columns and 11 profile columns | Years are stacked with all columns; missing columns left empty. Only indicators present in both years are used for comparisons. |
| 16 | Column name differs from schema | Data: `teacher_involve_non_training_assignment`; schema: `teachers_involve_...` | Kept the name used in the data. |
| 17 | State-wide drop in playgrounds | Playground availability fell more than 5 percentage points in 24 of 41 districts in a single year | Almost certainly a change in reporting, not real loss. Playground is excluded from the infrastructure index and from trend claims. |
| 18 | Unexplained toilet drop in one district | Jhalawar: functional girls' toilets down 5.6 points and boys' down 6.3 points in one year; no other district moved this much | Kept, flagged as needing verification before being reported as a finding. |
| 19 | Schools changing district between years | 189 schools changed district; 180 of them moved from Alwar to Kotputli-Behror | Confirmed boundary change. Year-on-year changes for Alwar and Kotputli-Behror reflect this, not real growth or decline; trend comparisons for these two use matched schools only. |
| 20 | Implausible staffing | Schools with students but no teachers (268 in 2024-25, 366 in 2025-26); pupil-teacher ratio above 60 (1,224 and 1,165 schools) | Kept and flagged (`flag_no_teachers_with_students`, `flag_high_ptr`), not silently dropped. |
| 21 | Schools in only one year | School codes are stable across years: 1,05,418 match; 884 appear only in 2024-25 and 1,027 only in 2025-26 | Treated as closures/mergers and new schools. School-level year-on-year comparisons use matched schools only. |
| 22 | Blank location names | LGD village fields are blank for urban schools and urban fields are blank for rural schools | Structural, not missing data. No action needed. |
