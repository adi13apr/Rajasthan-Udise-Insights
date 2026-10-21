"""Shared helpers for tests. Everything here builds synthetic data only."""
import zipfile


def make_zip(path, csv_text, inner="data.csv"):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(inner, csv_text)


# --- A small synthetic raw dataset (2024-25) with one planted problem per rule ---------
# Schools 101-105 in the state-wide file; 101 again in a "Kota test download".
PROFILE_COLS = ("pseudocode,state,district,block,rural_urban,school_category,school_type,"
                "lowclass,highclass,managment,lgd_block_name,anganwadi_yn")
PROFILE_ROWS = [
    "101,RAJASTHAN,KOTA,LADPURA,1,1,3,1,5,1,Ladpura,1",
    "102,RAJASTHAN,JALOR,AHORE,2,1,3,1,8,2,Ahore,9",      # old spelling -> JALORE
    "103,RAJASTHAN,NOWHERE,X,1,1,3,1,5,1,X,1",            # unknown district -> quarantine
    "104,RAJASTHAN,KOTA,JAIPUR REGION,1,1,3,1,12,77,,0",  # unknown management + code 0 -> flags
    "105,RAJASTHAN,,LADPURA,1,1,3,1,5,1,,1",              # no district -> quarantine
]
FACILITY_COLS = ("pseudocode,electricity_availability,hand_pump_fun_yn,well_prot_fun_yn,tap_fun_yn,"
                 "othsrc_fun_yn,pack_water_fun_yn,total_girls_toilet,total_girls_func_toilet,"
                 "func_girls_cwsn_friendly,total_boys_toilet,total_boys_func_toilet,func_boys_cwsn_friendly,"
                 "handwash_near_toilet,library_availability,playground_available,availability_ramps,internet,"
                 "laptop,desktop,tablet,total_class_rooms,classrooms_in_good_condition,"
                 "classrooms_needs_minor_repair,classrooms_needs_major_repair,solar_panel")
FACILITY_ROWS = [
    "101,1,1,2,2,2,2,2,1,0,2,2,0,9,1,1,1,2,0,1,0,0,3,1,1,3",   # solar_panel 3 -> flag; handwash 9
    "102,3,2,2,2,2,2,1,2,0,1,1,0,1,2,2,2,2,0,0,0,0,2,0,0,2",   # 2 functional of 1 girls' toilet -> flag
    "103,1,1,2,2,2,2,1,1,0,1,1,0,1,1,1,1,1,1,0,0,0,1,0,0,2",   # school quarantined -> orphan
    "104,1,1,2,2,2,2,1,1,0,1,1,0,1,1,1,1,1,x,0,0,0,1,0,0,2",   # laptop 'x' -> not_a_number
]
TEACHER_COLS = "pseudocode,total_tch,female,regular,contract,trained_cwsn,graduate,post_graduate_and_above"
TEACHER_ROWS = [
    "101,1,1,1,0,0,1,0",
    "102,0,0,0,0,0,0,0",   # 0 teachers, has students -> flag
    "104,1,0,1,0,0,0,1",   # 100 students per teacher -> flag high_ptr
    "106,-2,0,0,0,0,0,0",  # negative count -> quarantine (checked before orphan)
]
ENROL_COLS = "pseudocode,item_group,item_id,cpp_b,cpp_g," + ",".join(
    f"c{k}_{s}" for k in range(1, 13) for s in "bg")


def enrol_row(code, group, item, c1_b):
    return f"{code},{group},{item},0,0,{c1_b}," + ",".join(["0"] * 23)


ENROL_ROWS = [
    enrol_row(101, 1, 1, 5),
    enrol_row(101, 1, 1, 5),     # exact duplicate -> quarantine
    enrol_row(102, 1, 2, 7),
    enrol_row(104, 1, 1, 100),
    enrol_row(999, 1, 1, 3),     # no such school -> orphan
    enrol_row(101, 4, 21, 1),    # undocumented CWSN code -> enrolment_other, flag
    enrol_row(101, 4, 1, 1),
]


def csv(header, rows):
    return "\n".join([header] + rows) + "\n"


def write_sample_raw(raw, year="2024-25", profile_rows=PROFILE_ROWS):
    y = raw / year
    make_zip(y / f"profile_data_1_RAJASTHAN_{year}.zip", csv(PROFILE_COLS, profile_rows))
    make_zip(y / f"profile_data_1_KOTA_{year}.zip", csv(PROFILE_COLS, profile_rows[:1]))
    make_zip(y / f"facility_data_RAJASTHAN_{year}.zip", csv(FACILITY_COLS, FACILITY_ROWS))
    make_zip(y / f"teacher_data_RAJASTHAN_{year}.zip", csv(TEACHER_COLS, TEACHER_ROWS))
    make_zip(y / f"enrolment_data_1_KOTA_{year}.zip", csv(ENROL_COLS, ENROL_ROWS))


# Second year: school 101 moves from KOTA to BUNDI (like the ALWAR -> KOTPUTLI-BEHROR change).
PROFILE_ROWS_NEXT = [PROFILE_ROWS[0].replace(",KOTA,", ",BUNDI,")] + PROFILE_ROWS[1:]
