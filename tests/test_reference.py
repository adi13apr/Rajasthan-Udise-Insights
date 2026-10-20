"""Reference JSON files load and are internally consistent."""
from pipeline import config as C


def test_districts():
    assert len(C.DISTRICTS) == 41 and len(set(C.DISTRICTS)) == 41
    assert all(v in C.DISTRICTS for v in C.DISTRICT_ALIASES.values())
    assert not set(C.DISTRICT_ALIASES) & set(C.DISTRICTS)


def test_management_codes():
    # Validated grouping: 94, 95, 101 government; 97, 98 not (see CLAUDE.md).
    gov = {k for k, g in C.MANAGEMENT_GROUP.items() if g in C.GOVT_GROUPS}
    assert {94, 95, 101} <= gov and not {97, 98} & gov
    mgmt = C.load_reference("management_codes.json")["codes"]
    assert all(v["is_government"] == (v["group"] in C.GOVT_GROUPS) for v in mgmt.values())


def test_official_totals():
    assert C.OFFICIAL["2024-25"]["schools"] == 106_302
    assert C.OFFICIAL["2024-25"]["govt_teachers"] == 445_073


def test_schema_reference():
    s = C.load_reference("schema_columns.json")
    for ds in ("profile1", "facility", "teacher", "enrolment1"):
        assert set(s["datasets"][ds]) == {"2024-25", "2025-26"}
    fac = {c["column"]: c for c in s["datasets"]["facility"]["2025-26"]}
    assert fac["electricity_availability"]["codes"] == {"1": "Yes", "2": "No", "3": "Yes, but not functional"}
    cwsn = {i["item_id"] for i in s["enrolment1_items"]["2025-26"] if i["item_group"] == C.CWSN_GROUP}
    assert cwsn == C.CWSN_DOCUMENTED_IDS


def test_data_dictionary_covers_every_gold_column():
    """docs/data_dictionary.md lists every column of the real gold database (local run only)."""
    import duckdb
    import pytest
    if not C.GOLD_DB.exists():
        pytest.skip("needs the real gold database")
    doc = (C.ROOT / "docs" / "data_dictionary.md").read_text(encoding="utf-8")
    con = duckdb.connect(str(C.GOLD_DB), read_only=True)
    cols = con.execute("select table_name, column_name from information_schema.columns "
                       "where table_schema = 'main'").fetchall()
    con.close()
    missing = [f"{t}.{c}" for t, c in cols if f"## {t}" not in doc or f"| `{c}` |" not in doc]
    assert not missing, "run python scripts/make_data_dictionary.py: " + ", ".join(missing[:10])
