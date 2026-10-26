"""Generate SYNTHETIC raw zips with the UDISE+ file layout, for tests and CI.

    python scripts/make_sample_data.py            -> tests/fixtures/raw/<year>/*.zip

No real UDISE+ data is used or needed (the portal's terms do not allow
redistributing it). The schools, codes and numbers are made up, from a fixed
random seed, so every run writes byte-identical zips. File names, columns and
codes follow the real files: state-wide profile/facility/teacher files, one
enrolment file per district, transgender columns only from 2025-26, the 2024-25
spelling JALOR, schools moving from ALWAR to KOTPUTLI-BEHROR, and a "Kota test
download" that duplicates Kota schools.

Deliberately bad rows (one per error rule), and what the pipeline must do:
    9999001 district NOWHERE        -> schools: unknown_district; its facility,
                                       teacher and enrolment rows: orphan_school
    9999002 no district             -> schools: missing_required
    Kota test copies                -> duplicate_school_year (schools, facilities, teachers)
    a negative classroom count      -> facilities: negative_count
    total_tch = 'x'                 -> teachers: not_a_number
    enrolment for 9999003 (no such school) -> enrolment: orphan_school
plus warnings: solar_panel = 3 (invalid_code), item_group 4 / item_id 21
(undocumented_item), and Yes/No code 9 (valid: not applicable).
"""
import csv
import io
import json
import random
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "tests" / "fixtures" / "raw"
YEARS = ["2024-25", "2025-26"]
SEED = 42
SCHOOLS_PER_DISTRICT = 12
# District as written in each year's files (2024-25 used the old spelling JALOR).
DISTRICTS = {"AJMER": "AJMER", "KOTA": "KOTA", "JALORE": "JALOR", "ALWAR": "ALWAR",
             "KOTPUTLI-BEHROR": "KOTPUTLI-BEHROR"}
MOVED = 3  # ALWAR schools that belong to KOTPUTLI-BEHROR from 2025-26
MANAGEMENT = [1, 1, 2, 5, 5, 92, 94, 97]
LEVELS = [(1, 5, 1), (1, 8, 2), (1, 10, 3), (1, 12, 3), (6, 12, 5)]  # lowclass, highclass, category
FIXED_TIME = (2025, 1, 1, 0, 0, 0)

PROFILE_COLS = ["pseudocode", "state", "district", "block", "rural_urban", "lgd_urban_local_body_name",
                "lgd_vill_name", "lgd_block_name", "school_category", "school_type", "lowclass",
                "highclass", "managment", "pincode", "anganwadi_yn", "approachable_road", "estd_year"]
FACILITY_COLS = ["pseudocode", "electricity_availability", "hand_pump_fun_yn", "well_prot_fun_yn",
                 "tap_fun_yn", "othsrc_fun_yn", "pack_water_fun_yn", "total_girls_toilet",
                 "total_girls_func_toilet", "func_girls_cwsn_friendly", "total_boys_toilet",
                 "total_boys_func_toilet", "func_boys_cwsn_friendly", "handwash_near_toilet",
                 "library_availability", "playground_available", "availability_ramps", "internet",
                 "laptop", "desktop", "tablet", "total_class_rooms", "classrooms_in_good_condition",
                 "classrooms_needs_minor_repair", "classrooms_needs_major_repair", "solar_panel"]
TEACHER_COLS = ["pseudocode", "total_tch", "male", "female", "regular", "contract", "trained_cwsn",
                "graduate", "post_graduate_and_above", "class_taugt_pre_primary_only"]

EXPECTED_QUARANTINE = {
    "schools": {"missing_required": 1, "unknown_district": 1, "duplicate_school_year": 2 * SCHOOLS_PER_DISTRICT},
    "facilities": {"negative_count": 1, "duplicate_school_year": 2 * SCHOOLS_PER_DISTRICT, "orphan_school": 1},
    "teachers": {"not_a_number": 1, "duplicate_school_year": 2 * SCHOOLS_PER_DISTRICT, "orphan_school": 1},
    "enrolment": {"orphan_school": 5},   # 4 social-category rows of 9999001 + 1 row of 9999003
}


def enrol_cols(year: str) -> list[str]:
    sexes = "bgt" if year >= "2025-26" else "bg"   # transgender columns start in 2025-26
    return (["pseudocode", "item_group", "item_id"] + [f"cpp_{s}" for s in sexes]
            + [f"c{k}_{s}" for k in range(1, 13) for s in sexes])


def schools(rng: random.Random) -> list[dict]:
    out = []
    for d_i, district in enumerate(DISTRICTS):
        if district == "KOTPUTLI-BEHROR":
            continue  # its schools come from ALWAR (MOVED) plus its own below
        for i in range(SCHOOLS_PER_DISTRICT):
            low, high, cat = rng.choice(LEVELS)
            out.append({"pseudocode": 9100000 + d_i * 1000 + i, "district": district,
                        "block": f"{district[:4]}-BLOCK-{i % 3 + 1}", "rural_urban": rng.choice([1, 1, 2]),
                        "lowclass": low, "highclass": high, "school_category": cat,
                        "school_type": rng.choice([1, 2, 3, 3, 3]), "managment": rng.choice(MANAGEMENT)})
    for i in range(4):  # KOTPUTLI-BEHROR's own schools
        out.append({**out[0], "pseudocode": 9105000 + i, "district": "KOTPUTLI-BEHROR", "block": "KOTP-BLOCK-1"})
    return out


def district_in(school: dict, year: str) -> str:
    """District as written in that year's files (boundary change and old spellings)."""
    d = school["district"]
    if d == "ALWAR" and year >= "2025-26" and school["pseudocode"] % 1000 < MOVED:
        d = "KOTPUTLI-BEHROR"
    return DISTRICTS.get(d, d) if year < "2025-26" else d


def yn(rng, p_yes=0.7, p_na=0.05):
    r = rng.random()
    return 9 if r < p_na else 1 if r < p_na + p_yes else 2


def profile_row(rng, s, year):
    urban = s["rural_urban"] == 2
    return [s["pseudocode"], "RAJASTHAN", district_in(s, year), s["block"], s["rural_urban"],
            "Sample Municipal Body" if urban else "", "" if urban else "Sample Village",
            s["block"].title(), s["school_category"], s["school_type"], s["lowclass"], s["highclass"],
            s["managment"], 300000 + s["pseudocode"] % 1000, yn(rng), yn(rng),
            1950 + s["pseudocode"] % 70]


def facility_row(rng, s):
    good, minor, major = rng.randint(1, 10), rng.randint(0, 3), rng.randint(0, 2)
    girls, boys = rng.randint(0, 4), rng.randint(0, 4)
    return [s["pseudocode"], rng.choice([1, 1, 1, 2, 3]), yn(rng), yn(rng, 0.2), yn(rng, 0.5), 2, yn(rng, 0.1),
            girls, rng.randint(0, girls), 0, boys, rng.randint(0, boys), 0, yn(rng), yn(rng), yn(rng),
            yn(rng, 0.5), yn(rng, 0.4), rng.randint(0, 3), rng.randint(0, 2), rng.randint(0, 2),
            0, good, minor, major, yn(rng, 0.1, 0)]


def teacher_row(rng, s):
    total = rng.choice([1, 2, 3, 5, 8, 12, 20])
    female = rng.randint(0, total)
    regular = rng.randint(0, total)
    graduate = rng.randint(0, total)
    return [s["pseudocode"], total, total - female, female, regular, total - regular, rng.randint(0, 1),
            graduate, total - graduate, 0]   # pre-primary-only teachers: 0, so no extra random draw


def enrolment_rows(rng, s, year):
    cols = enrol_cols(year)
    rows = []
    for item in range(1, 5):  # item_group 1: General, SC, ST, OBC
        row = {"pseudocode": s["pseudocode"], "item_group": 1, "item_id": item}
        for c in cols[3:]:
            grade = c.split("_")[0][1:]
            in_school = grade == "pp" or s["lowclass"] <= int(grade) <= s["highclass"]
            if c.endswith("_t"):  # transgender students are rare
                row[c] = rng.randint(1, 2) if in_school and rng.random() < 0.02 else 0
            else:
                row[c] = rng.randint(0, 12) if in_school else 0
        rows.append([row[c] for c in cols])
    if s["pseudocode"] % 4 == 0:  # a few children with disabilities (item_group 4)
        rows.append([s["pseudocode"], 4, 1 + s["pseudocode"] % 20, 0, 0] + [0] * (len(cols) - 5))
        rows[-1][cols.index("c1_b")] = 1
    return rows


def to_zip(path: Path, header: list[str], rows: list[list]) -> None:
    assert all(len(r) == len(header) for r in rows), path.name
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(header)
    w.writerows(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    info = zipfile.ZipInfo(path.stem + ".csv", date_time=FIXED_TIME)  # fixed time -> identical bytes
    info.compress_type = zipfile.ZIP_DEFLATED
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(info, buf.getvalue())


def make_sample(out: Path = OUT) -> dict:
    rng = random.Random(SEED)
    all_schools = schools(rng)
    for year in YEARS:
        prof, fac, tch = [], [], []
        enrol: dict[str, list] = {}
        y = year[:4]
        for s in all_schools:
            # school 9100000 has students only in 2025-26 (a zero-enrolment school in 2024-25)
            prof.append(profile_row(rng, s, year))
            fac.append(facility_row(rng, s))
            tch.append(teacher_row(rng, s))
            if not (s["pseudocode"] == 9100000 and year == "2024-25"):
                enrol.setdefault(district_in(s, year), []).extend(enrolment_rows(rng, s, year))
        if year == "2024-25":
            bad = {**all_schools[0], "pseudocode": 9999001, "district": "NOWHERE"}
            prof.append(profile_row(rng, bad, year))
            fac.append(facility_row(rng, bad))
            tch.append(teacher_row(rng, bad))
            enrol["AJMER"].extend(enrolment_rows(rng, bad, year)[:4])
            prof.append(profile_row(rng, {**all_schools[0], "pseudocode": 9999002, "district": ""}, year))
            enrol["AJMER"].append([all_schools[1]["pseudocode"], 4, 21]
                                  + [0] * (len(enrol_cols(year)) - 3))   # undocumented CWSN code
        else:
            fac[1][FACILITY_COLS.index("classrooms_needs_major_repair")] = -1   # negative count
            tch[2][TEACHER_COLS.index("total_tch")] = "x"                     # not a number
            enrol["AJMER"].append([9999003, 1, 1] + [1] * (len(enrol_cols(year)) - 3))  # orphan
        fac[3][FACILITY_COLS.index("solar_panel")] = 3                          # undocumented code
        kota = [i for i, s in enumerate(all_schools) if s["district"] == "KOTA"]
        d = out / year
        to_zip(d / f"profile_data_1_RAJASTHAN_{year}.zip", PROFILE_COLS, prof)
        to_zip(d / f"facility_data_RAJASTHAN_{year}.zip", FACILITY_COLS, fac)
        to_zip(d / f"teacher_data_RAJASTHAN_{year}.zip", TEACHER_COLS, tch)
        # the "Kota test download": the same Kota rows again
        to_zip(d / f"profile_data_1_KOTA_{year}.zip", PROFILE_COLS, [prof[i] for i in kota])
        to_zip(d / f"facility_data_KOTA_{year}.zip", FACILITY_COLS, [fac[i] for i in kota])
        to_zip(d / f"teacher_data_KOTA_{year}.zip", TEACHER_COLS, [tch[i] for i in kota])
        for district, rows in sorted(enrol.items()):
            to_zip(d / f"enrolment_data_1_{district}_{year}.zip", enrol_cols(year), rows)
    expected = {"seed": SEED, "quarantine": EXPECTED_QUARANTINE,
                "schools_per_year_in_silver": {"2024-25": len(all_schools), "2025-26": len(all_schools)},
                "district_moves": {"ALWAR -> KOTPUTLI-BEHROR": MOVED}}
    (out / "expected.json").write_text(json.dumps(expected, indent=2), encoding="utf-8")
    return expected


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else OUT
    make_sample(target)
    print(f"Synthetic sample written to {target}")
