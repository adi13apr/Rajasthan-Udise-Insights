"""RAG phase 3: the safe SQL tool (rag/sql_tool.py) and the schema card (rag/schema_card.py).

Three kinds of tests:
- the SQL checks alone (no database): every unsafe pattern is rejected, safe ones pass, LIMIT;
- on the synthetic sample gold database (built here, as in CI): running, hidden school codes,
  the database's own lock-down, timeout, examples, validation SQL equal to the pipeline's;
- on the full gold database, skipped when it is absent (CI): the 8 official 2024-25 figures
  reproduced exactly through the tool, and the committed schema card is up to date.
"""
from pathlib import Path

import duckdb
import pytest

pytest.importorskip("sqlglot")

from pipeline import config as C  # noqa: E402
from pipeline.bronze import load_bronze  # noqa: E402
from pipeline.gold import build_gold  # noqa: E402
from pipeline.silver import build_silver  # noqa: E402
from pipeline.validation import compare, compute_indicators  # noqa: E402
from rag import schema_card as SC  # noqa: E402
from rag.sql_tool import (ALLOWED_TABLES, MAX_ROWS, SqlExecutionError, SqlRejected,  # noqa: E402
                          SqlTimeout, SqlTool, check_sql, default_gold_db)
from scripts.make_sample_data import make_sample  # noqa: E402

VALIDATION_SQL = [C.ROOT / "sql" / "validation" / "01_official_indicators.sql",       # via int_school_year
                  C.ROOT / "sql" / "powerbi" / "check_validation_measures.sql"]       # facts directly

# ----------------------------------------------------------------------- 1. the checks alone
UNSAFE = {
    # not exactly one statement
    "empty": "",
    "blank": "   \n ",
    "two selects": "select 1; select 2",
    "select then drop": "select * from dim_year; drop table dim_year",
    "second statement after a comment": "select 1 /* harmless */; delete from dim_year",
    "attach hidden after a line comment": "select 1 -- note\n; attach 'x.duckdb' as x",
    # DML / DDL
    "insert": "insert into dim_year values (3, '2026-27', 2026)",
    "update": "update dim_year set start_year = 1",
    "delete": "delete from dim_year",
    "drop": "drop table dim_year",
    "create table as": "create table x as select * from dim_year",
    "create temp view": "create temp view v as select 1",
    "alter": "alter table dim_year add column x int",
    "truncate": "truncate dim_year",
    "delete inside a cte": "with d as (delete from dim_year returning *) select * from d",
    # commands
    "attach": "attach 'other.duckdb' as other",
    "detach": "detach udise",
    "copy table to file": "copy dim_year to 'out.csv'",
    "copy query to file": "copy (select * from dim_school) to 'out.parquet' (format parquet)",
    "copy from file": "copy dim_year from 'in.csv'",
    "pragma": "pragma database_list",
    "install": "install httpfs",
    "load": "load httpfs",
    "set": "set threads = 1",
    "use": "use udise",
    "begin": "begin transaction",
    "checkpoint": "checkpoint",
    "vacuum": "vacuum",
    "export database": "export database 'dump'",
    "call": "call pragma_version()",
    "explain": "explain select 1",
    "describe": "describe dim_year",
    "show": "show tables",
    "summarize": "summarize dim_year",
    # file-reading and system functions
    "read_csv": "select * from read_csv('x.csv')",
    "read_csv_auto": "select * from read_csv_auto('x.csv')",
    "read_parquet": "select * from read_parquet('data/gold/parquet/*.parquet')",
    "read_json_auto": "select * from read_json_auto('x.json')",
    "read_text scalar": "select read_text('C:/Windows/win.ini')",
    "read_blob": "select content from read_blob('x.bin')",
    "parquet_scan": "select * from parquet_scan('x.parquet')",
    "glob": "select * from glob('*')",
    "query": "select * from query('select * from dim_year')",
    "sniff_csv": "select * from sniff_csv('x.csv')",
    "duckdb_tables": "select * from duckdb_tables()",
    "duckdb_settings": "select * from duckdb_settings()",
    "current_setting": "select current_setting('threads')",
    "file as a table": "select * from 'x.csv'",
    "parquet glob as a table": "select * from 'data/gold/parquet/*.parquet'",
    "range table function": "select * from range(10)",
    "read_csv nested in a subquery": "select * from dim_year where year_key in (select 1 from read_csv('x'))",
    "read_parquet in a cte": "with x as (select * from read_parquet('a.parquet')) select * from x",
    "read_text in a scalar subquery": "select (select read_text('x')) as t from dim_year",
    # tables outside the allowlist
    "unknown table": "select * from secrets",
    "information_schema": "select * from information_schema.tables",
    "sqlite_master": "select * from sqlite_master",
    "schema-qualified": "select * from main.dim_year",
    "catalog-qualified": "select * from udise.main.dim_school",
    "allowed table joined to an unknown one": "select * from dim_year join other using (year_key)",
    # the school code
    "pseudocode": "select pseudocode from dim_school",
    "qualified pseudocode": "select s.pseudocode from int_school_year s",
    "pseudocode in an aggregate": "select count(distinct pseudocode) from dim_school",
    "pseudocode in where": "select count(*) from dim_school where pseudocode > 0",
    # not parseable
    "garbage": "this is not sql",
}

SAFE = [
    "select * from dim_year",
    "SELECT academic_year FROM DIM_YEAR",                                  # case-insensitive
    'select * from "dim_year"',
    "from dim_year",                                                       # DuckDB from-first
    "select 1",
    "select 1;",                                                           # trailing semicolon
    "select 1; -- trailing comment",
    "with t as (select * from dim_year) select * from t",
    "with dim_year as (select 1 as year_key) select * from dim_year",     # CTE shadowing a table
    "select year_key from dim_year union all select year_key from dim_year",
    "select * from (select district_name from dim_district) d",
    "select * from (values (1), (2)) v(a)",
    "select count(*) filter (where has_electricity = 0) from int_school_year",
    "select district_name, list_contains(aliases, 'JALOR') from dim_district",
]


@pytest.mark.parametrize("sql", UNSAFE.values(), ids=UNSAFE.keys())
def test_unsafe_sql_is_rejected(sql):
    with pytest.raises(SqlRejected):
        check_sql(sql)


@pytest.mark.parametrize("sql", SAFE)
def test_safe_sql_passes(sql):
    executed, _ = check_sql(sql)
    assert executed.upper().rstrip().endswith(f"LIMIT {MAX_ROWS}")


def test_rejection_messages_say_why():
    cases = {"select 1; select 2": "exactly one statement",
             "drop table dim_year": "only SELECT",
             "select * from read_csv('x')": "table functions",
             "select read_text('x')": "read_text",
             "select * from main.dim_year": "qualified",
             "select * from secrets": "not allowed",
             "select pseudocode from dim_school": "school code"}
    for sql, words in cases.items():
        with pytest.raises(SqlRejected, match=words):
            check_sql(sql)


@pytest.mark.parametrize("sql, expected_limit, applied", [
    ("select * from dim_year", "LIMIT 200", True),
    ("select * from dim_year limit 50", "LIMIT 50", False),
    ("select * from dim_year limit 200", "LIMIT 200", False),
    ("select * from dim_year limit 5000", "LIMIT 200", True),
    ("select * from dim_year limit 10 offset 5", "LIMIT 10 OFFSET 5", False),
    ("select * from dim_year limit 5000 offset 5", "LIMIT 200 OFFSET 5", True),
    ("select * from dim_year limit 50 percent", "LIMIT 200", True),
    ("select * from dim_year limit (select 1000)", "LIMIT 200", True),
    ("select * from dim_year fetch first 300 rows only", "LIMIT 200", True),
    ("select 1 union select 2 limit 1000", "LIMIT 200", True),
    ("select * from (select * from dim_year limit 5) t", "LIMIT 200", True),  # inner limit is not the outer
])
def test_limit_is_added_or_lowered(sql, expected_limit, applied):
    executed, limit_applied = check_sql(sql)
    assert executed.endswith(expected_limit) and limit_applied is applied


# ----------------------------------------------------------- 2. on the synthetic sample gold
@pytest.fixture(scope="module")
def sample_db(tmp_path_factory):
    d = tmp_path_factory.mktemp("rag_sql")
    make_sample(d / "raw")
    load_bronze(d / "raw", d / "bronze", run_id="r1")
    build_silver(d / "bronze", d / "silver", d / "quarantine", run_id="r1")
    build_gold(d / "silver", d / "udise.duckdb", d / "parquet", d / "exports")
    return d / "udise.duckdb"


@pytest.fixture(scope="module")
def sample_expected(sample_db):
    """Computed before any tool is open: DuckDB refuses a second connection to the same file
    in one process with different settings (the tool's are locked down)."""
    return {"indicators": compute_indicators(sample_db), "columns": _visible_columns(sample_db),
            "card": SC.build_card(sample_db)}


@pytest.fixture
def tool(sample_db):
    with SqlTool(sample_db) as t:
        yield t


def test_run_returns_columns_rows_count_and_sql(tool):
    r = tool.run("select academic_year, start_year from dim_year order by 1")
    assert r.columns == ["academic_year", "start_year"]
    assert r.rows == [("2024-25", 2024), ("2025-26", 2025)] and r.row_count == 2
    assert r.sql_executed == "SELECT academic_year, start_year FROM dim_year ORDER BY 1 LIMIT 200"
    assert r.limit_applied and not r.truncated and r.elapsed_ms >= 0
    assert r.as_dict()["rows"] == [["2024-25", 2024], ["2025-26", 2025]]


def test_row_cap_is_reported(tool):
    r = tool.run("select a.grade_key, b.grade_key from dim_grade a, dim_grade b")   # 13 x 13 = 169
    assert r.row_count == 169 and not r.truncated
    r = tool.run("select a.grade_key from dim_grade a, dim_grade b, dim_grade c")     # 2,197 rows
    assert r.row_count == MAX_ROWS and r.truncated and r.notes


@pytest.mark.parametrize("sql", list(UNSAFE.values())[:20] + ["select * from read_csv('x.csv')",
                                                               "select pseudocode from dim_school"])
def test_tool_rejects_before_running(tool, sql):
    with pytest.raises(SqlRejected):
        tool.run(sql)


def test_school_code_is_hidden_even_from_select_star(tool):
    for table in ("dim_school", "int_school_year"):
        assert "pseudocode" not in tool.run(f"select * from {table} limit 1").columns
    assert "school_key" in tool.run("select * from dim_school limit 1").columns


def test_database_refuses_writes_and_files_even_without_the_checks(tool, tmp_path):
    """Layer 2: statements sent straight to the connection, bypassing check_sql."""
    target = (tmp_path / "out.csv").as_posix()
    raw = [f"copy (select * from dim_year) to '{target}'",
           f"select * from read_csv('{target}')",
           f"attach '{(tmp_path / 'x.duckdb').as_posix()}' as x",
           "install httpfs", "load httpfs",
           "set enable_external_access = true", "set threads = 1",
           "insert into dim_year values (9, 'x', 1)",
           "create table main.x as select 1",
           "select pseudocode from dim_school"]
    for sql in raw:
        with pytest.raises(duckdb.Error):
            tool.con.execute(sql)
    assert not (tmp_path / "out.csv").exists() and not (tmp_path / "x.duckdb").exists()


def test_execution_errors_carry_duckdb_message(tool):
    with pytest.raises(SqlExecutionError, match="not found"):
        tool.run("select no_such_column from dim_year")


def test_timeout_stops_a_long_query_and_the_tool_still_works(sample_db):
    with SqlTool(sample_db, timeout_s=0.5) as t:
        slow = "select count(*) from " + ", ".join(f"dim_grade g{i}" for i in range(10))  # 13^10 rows
        with pytest.raises(SqlTimeout):
            t.run(slow)
        assert t.run("select count(*) from dim_year").rows == [(2,)]


def test_missing_database_is_a_clear_error(tmp_path):
    with pytest.raises(FileNotFoundError, match="UDISE_GOLD_DB"):
        SqlTool(tmp_path / "none.duckdb")


def test_gold_db_path_comes_from_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("UDISE_GOLD_DB", str(tmp_path / "g.duckdb"))
    assert default_gold_db() == tmp_path / "g.duckdb"


@pytest.mark.parametrize("question, sql", SC.EXAMPLES, ids=[f"example{i}" for i in range(1, 6)])
def test_schema_card_examples_run(tool, question, sql):
    r = tool.run(sql)
    assert r.columns and r.row_count >= 1


@pytest.mark.parametrize("path", VALIDATION_SQL, ids=lambda p: p.name)
def test_validation_sql_through_the_tool_equals_the_pipeline(sample_expected, tool, path):
    got = tool.run(path.read_text(encoding="utf-8"))
    expected = sample_expected["indicators"]
    assert sorted(map(tuple, got.rows)) == sorted(map(tuple, expected[["year", "indicator", "value"]]
                                                       .itertuples(index=False, name=None)))


def _visible_columns(db):
    con = duckdb.connect(str(db), read_only=True)
    try:
        return {(t, c) for t, c in con.execute(
            "select table_name, column_name from information_schema.columns where table_schema = 'main'"
        ).fetchall() if t in ALLOWED_TABLES and c != "pseudocode"}
    finally:
        con.close()


def test_schema_card_covers_every_table_and_column(sample_expected):
    built = sample_expected["card"]
    committed = SC.OUT.read_text(encoding="utf-8")
    for card in (built, committed):
        for table in ALLOWED_TABLES:
            assert f"### {table}" in card
        for _, column in sample_expected["columns"]:
            assert f"`{column}`" in card, column
        assert "pseudocode" not in card
        for code in ("94", "95", "101", "4,45,073"):
            assert code in card
        assert card.count("```sql") == 5


# -------------------------------------------------- 3. full gold database (skipped in CI)
FULL_DB = default_gold_db()
needs_full_db = pytest.mark.skipif(not FULL_DB.exists(),
                                   reason=f"full gold database not found ({FULL_DB}); set UDISE_GOLD_DB")


@pytest.fixture
def full_tool():
    with SqlTool(FULL_DB) as t:
        yield t


@needs_full_db
@pytest.mark.parametrize("path", VALIDATION_SQL, ids=lambda p: p.name)
def test_full_db_reproduces_the_8_official_figures(full_tool, path):
    import pandas as pd
    r = full_tool.run(path.read_text(encoding="utf-8"))
    ours = pd.DataFrame(r.rows, columns=r.columns)
    table = compare(ours, C.load_reference("official_totals.json")["totals"])
    checked = table[table["result"] != "no official figure"]
    assert len(checked) == 8
    assert (checked["result"] == "exact match").all(), checked.to_string()


@needs_full_db
def test_full_db_first_example_gives_official_totals(full_tool):
    official = C.load_reference("official_totals.json")["totals"]["2024-25"]
    r = full_tool.run(SC.EXAMPLES[0][1])
    assert r.rows == [(official["schools"], official["enrolment"], official["teachers"])]


@needs_full_db
def test_committed_schema_card_is_up_to_date():
    assert SC.build_card(FULL_DB) == SC.OUT.read_text(encoding="utf-8"), \
        "rag/schema_card.md is stale: run python -m rag.schema_card"
