# Quality rules

Generated from `pipeline/rules.py` (`python -m pipeline.rules`). Do not edit by hand.

- **error**: the row is moved to `data/quarantine/<table>.parquet` with `_rule`, `_reason` and `_run_id`. Error rules run in this order; a row is quarantined by the first one it fails.
- **warning**: the row stays in silver with a `flag_<rule>` column (1 = fails).

| Rule | Severity | Tables | What it checks |
|---|---|---|---|
| `missing_required` | error | schools, facilities, teachers, enrolment, enrolment_other | Key fields are present: year and pseudocode (and district for schools). |
| `not_a_number` | error | schools, facilities, teachers, enrolment, enrolment_other | Every column the schema says is numeric holds a whole number. |
| `negative_count` | error | schools, facilities, teachers, enrolment, enrolment_other | Counts (enrolment, teachers, rooms, toilets, ...) are not negative. |
| `unknown_district` | error | schools | District, after mapping old spellings, is one of the 41 districts (reference/districts.json). |
| `duplicate_school_year` | error | schools, facilities, teachers | One row per (year, pseudocode). Extra copies (e.g. Kota schools also in the Kota test download) are quarantined; the copy from the state-wide file is kept. |
| `duplicate_enrolment_row` | error | enrolment, enrolment_other | One row per (year, pseudocode, item_group, item_id). |
| `orphan_school` | error | facilities, teachers, enrolment, enrolment_other | The row's school exists in silver schools for the same year. |
| `unknown_management` | warning | schools | Management code is in reference/management_codes.json (else grouped as 'Unmapped'). |
| `invalid_rural_urban` | warning | schools | rural_urban is 1 (Rural) or 2 (Urban). |
| `invalid_code` | warning | schools, facilities | Coded columns use a code documented in the schema (Yes/No columns: 1, 2 or 9 = not applicable). Undocumented codes count as missing in derived indicators. |
| `functional_toilets_exceed_total` | warning | facilities | Functional toilets (girls, boys) are not more than total toilets. |
| `classroom_total_mismatch` | warning | facilities | Where total_class_rooms is filled in, it equals good + minor repair + major repair. |
| `no_teachers_with_students` | warning | teachers | A school with students in classes 1-12 has at least one teacher. |
| `high_ptr` | warning | teachers | Pupil-teacher ratio (classes 1-12) is at most 60. |
| `undocumented_item` | warning | enrolment_other | (item_group, item_id) is documented in the schema (item_id 21 in group 4 is not). |
