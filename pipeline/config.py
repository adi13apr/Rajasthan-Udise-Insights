"""Project-wide settings: paths, dataset names and UDISE+ code mappings.

Lookup data (district names, management codes, official totals, schema) lives in
reference/*.json and is loaded here, so the rest of the code uses the same names.
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = ROOT / "data" / "raw"
PROCESSED_DIR = ROOT / "data" / "processed"
REPORTS_DIR = ROOT / "reports"
REFERENCE_DIR = ROOT / "reference"
PROFILING_DIR = REPORTS_DIR / "profiling"
BRONZE_DIR = ROOT / "data" / "bronze"
# Bronze keeps its own state (checksum of each source zip) next to the data, so
# deleting data/bronze/ also resets incremental loading.
BRONZE_STATE = BRONZE_DIR / "_state.json"
RUN_MANIFEST = REPORTS_DIR / "run_manifest.json"


def load_reference(name: str) -> dict:
    return json.loads((REFERENCE_DIR / name).read_text(encoding="utf-8"))


# Zip file-name prefix (as downloaded from microdata.udiseplus.gov.in) -> dataset key.
# File names look like: facility_data_KOTA_2024-25.zip
DATASETS = {
    "profile_data_1": "profile1",
    "profile_data_2": "profile2",
    "facility_data": "facility",
    "teacher_data": "teacher",
    "enrolment_data_1": "enrolment1",
    "enrolment_data_2": "enrolment2",
    "safety": "safety",  # only published from 2025-26
}

# Hook for renaming columns if a future release changes names.
# Note: the data uses `teacher_involve_non_training_assignment`, while the
# schema PDF says `teachers_involve_...`. We keep the name used in the data.
COLUMN_RENAMES: dict[str, str] = {}

# The portal spells some districts differently in different years.
# Map every variant to one name so years can be compared (reference/districts.json).
_districts = load_reference("districts.json")
DISTRICTS: list[str] = _districts["canonical"]  # the 41 districts
DISTRICT_ALIASES: dict[str, str] = _districts["aliases"]

# --- Code mappings (from the DSP schema PDF unless marked VERIFY) ---------

# Yes/No columns: 1 = Yes, 2 = No. 9 appears in the data for questions that
# do not apply to a school (e.g. SMC for private schools) -> treated as missing.
YES_NO = {1: 1, 2: 0}
NOT_APPLICABLE = 9

# Checked against the data: rural_urban = 1 rows have village names,
# rural_urban = 2 rows have urban local body names.
RURAL_URBAN = {1: "Rural", 2: "Urban"}

# Standard UDISE+ convention. VERIFY against the UDISE+ DCF.
SCHOOL_TYPE = {1: "Boys", 2: "Girls", 3: "Co-educational"}

# Management codes are "as per UDISE DCF" and not listed in the schema.
# Grouping (validated against official totals) in reference/management_codes.json.
_management = load_reference("management_codes.json")
MANAGEMENT_GROUP: dict[int, str] = {int(k): v["group"] for k, v in _management["codes"].items()}
GOVT_GROUPS: list[str] = _management["government_groups"]

# Class bands used throughout the analysis.
STAGES = {
    "primary": range(1, 6),          # classes 1-5
    "upper_primary": range(6, 9),    # classes 6-8
    "secondary": range(9, 11),       # classes 9-10
    "higher_secondary": range(11, 13),  # classes 11-12
}

# Enrolment_1: item_group 1 (General/SC/ST/OBC) is a complete split of all
# students, so summing it gives total enrolment without double counting.
ENROL_TOTAL_GROUP = 1
SOCIAL_CATEGORY = {1: "General", 2: "SC", 3: "ST", 4: "OBC"}
CWSN_GROUP = 4
CWSN_DOCUMENTED_IDS = set(range(1, 21))  # schema documents ids 1-20 only

# Safe drinking-water sources (functional). Unprotected wells are excluded.
SAFE_WATER_COLS = [
    "hand_pump_fun_yn", "well_prot_fun_yn", "tap_fun_yn",
    "othsrc_fun_yn", "pack_water_fun_yn",
]

# Published state totals used to validate the pipeline (reference/official_totals.json).
OFFICIAL: dict[str, dict[str, int]] = load_reference("official_totals.json")["totals"]

# Pupil-teacher ratio above this is flagged (RTE norm is 30:1 at primary).
PTR_FLAG = 60
