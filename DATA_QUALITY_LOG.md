# Data Quality Log

Decisions taken while cleaning UDISE+ school-level data. Automated checks are
regenerated on every run in `reports/qa_report.md`; this file records what was
found and what was done about it.

Source: UDISE+ Data Sharing Portal (microdata.udiseplus.gov.in), school-level
CSVs for 2024-25 and 2025-26. Schemas: `docs/DSP_Schema_2024-25.pdf`, `docs/DSP_Schema_2025-26.pdf`.

| # | Issue found | Evidence | Decision |
|---|---|---|---|
| 1 | `total_class_rooms` is 0 for **every** school in the 2024-25 release | 2,265 of 2,265 Kota schools are 0, yet most report classrooms by condition | Classroom count rebuilt as good + minor repair + major repair. In 2025-26, wherever `total_class_rooms` is filled in, this sum matches it exactly (100% of rows). |
| 2 | Yes/No questions use a third code, `9` | e.g. `smc_exists` = 9 for every private school; SMCs are only required for govt/aided schools | `9` treated as "not applicable" (missing), never as "No". |
| 3 | Central-government schools list an admin region as their block | In 2025-26, 3 Kota schools (codes 92, 93) have block = "JAIPUR REGION"; in 2024-25 the same codes had real block names | Kept in the data, `block_clean` left blank and flagged with `block_is_admin_region`. District totals are unaffected. |
| 4 | Management codes are not documented in the schema | Codes 1, 2, 3, 5, 6, 90, 92, 93, 97 appear | Provisional groups in `pipeline/config.py`, inferred from the data (SMC presence, block field). Code 97 kept as "Other / Unverified". **To do: verify against the UDISE+ DCF.** |
| 5 | Undocumented disability code | Enrolment item_group 4 contains item_id 21; schema lists only 1-20 | Included in the CWSN total; flagged in the QA report. |
| 6 | Schema changes between years | 2025-26 adds transgender enrolment columns (`*_t`), 12 new facility columns, 11 new profile columns and a new safety file | Pipeline stacks years with all columns; columns missing in a year are left empty. Transgender students are counted in totals where reported. |
| 7 | Column name differs from schema | Data: `teacher_involve_non_training_assignment`; schema: `teachers_involve_...` | Kept the name used in the data (same in both years). |
| 8 | Suspicious drop in playground availability | 231 schools reported a playground in 2024-25 but not in 2025-26 (only 40 went the other way); district share fell 8.4 points | Likely a reporting change rather than real loss. Playground is excluded from the infrastructure index and from year-on-year trend claims. |
| 9 | Some schools have no enrolment rows | 4 schools in 2024-25 and 1 in 2025-26 appear in profile files but not in enrolment | Enrolment set to 0 and flagged; schools with 0 enrolment are excluded from district summaries. |
| 10 | Implausible staffing | Schools with students but 0 teachers (3 in 2024-25, 6 in 2025-26); pupil-teacher ratio above 60 (16 and 15 schools) | Kept and flagged (`flag_no_teachers_with_students`, `flag_high_ptr`) for review, not silently dropped. |
| 11 | Schools appearing in only one year | School codes (`pseudocode`) are stable across years: 2,233 schools match; 32 appear only in 2024-25 and 22 only in 2025-26 | Treated as closures/mergers and new schools. Year-on-year school-level comparisons use matched schools only. |
| 12 | Many blank location names | LGD village fields are blank for urban schools and urban fields are blank for rural schools | Structural, not missing data. No action needed. |
| 13 | Validation against official totals | — | **To do:** compare state totals with the published UDISE+ 2024-25 and 2025-26 reports once all districts are loaded. |
