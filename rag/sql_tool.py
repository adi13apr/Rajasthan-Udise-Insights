"""A safe, read-only SQL tool over the gold tables (RAG phase 3). No LLM here: later phases
let the agent call `SqlTool.run(sql)` and read `rag/schema_card.md` to write the SQL.

Two independent layers of protection, so a gap in one is caught by the other:

1. **The SQL is checked before it runs** (`check_sql`, parsed with sqlglot, DuckDB dialect):
   exactly one statement; the top level must be a query (SELECT / UNION / INTERSECT / EXCEPT,
   CTEs allowed); no DDL, DML or commands anywhere in the tree (ATTACH, COPY, PRAGMA, SET,
   INSTALL, LOAD, ...); every table is an unqualified name from the allowlist or a CTE of the
   same query (no `main.x`, no `FROM 'file.csv'`, no table functions such as read_csv(...));
   no file-reading or system functions anywhere; the school code `pseudocode` is never
   referenced. The query that runs is the one sqlglot re-generates from the checked tree, so
   what was checked is exactly what runs (a comment or odd quoting cannot hide anything).
   A LIMIT of 200 is added if missing, and a larger LIMIT is lowered to 200.

2. **The database itself refuses anything but reading**: opened `read_only=True` with
   `enable_external_access = false` (no file system, no ATTACH, no COPY, no extensions; DuckDB
   does not allow switching it back on while the database is open) and `lock_configuration =
   true` (no SET can change any setting afterwards). `pseudocode` is hidden by temporary views
   with the same names as the tables, so even a query that slipped past layer 1 could not
   return it (UDISE+ terms: no re-identification of schools).

A query that runs longer than the timeout is stopped with DuckDB's interrupt.

    from rag.sql_tool import SqlTool
    with SqlTool() as tool:
        r = tool.run("select district_name, count(*) from int_school_year group by 1")
        r.columns, r.rows, r.row_count, r.sql_executed

The database path comes from `UDISE_GOLD_DB`; see `default_gold_db()`.
"""
from __future__ import annotations

import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import duckdb
import sqlglot
from sqlglot import exp

from .config import ROOT

MAX_ROWS = 200
DEFAULT_TIMEOUT_S = 10.0
MEMORY_LIMIT = "2GB"
THREADS = 4

# Every table and view of the gold database that the tool may read.
ALLOWED_TABLES = frozenset({
    "dim_year", "dim_district", "dim_management", "dim_school", "dim_grade", "dim_gender",
    "dim_social_category", "fact_enrolment", "fact_teachers", "fact_school_facilities",
    "int_school_year", "mart_district_year", "mart_district_year_management",
})
HIDDEN_COLUMNS = frozenset({"pseudocode"})  # the school code: never returned, never referenced

# Functions that read files, reach outside the database or reveal its internals. Matched on the
# lower-case name; PREFIXES catch whole families (read_csv_auto, duckdb_tables, pragma_version...).
DENIED_FUNCTIONS = frozenset({
    "glob", "query", "query_table", "sniff_csv", "parquet_scan", "parquet_metadata",
    "parquet_schema", "parquet_file_metadata", "parquet_kv_metadata", "csv_scan", "json_scan",
    "iceberg_scan", "iceberg_metadata", "iceberg_snapshots", "delta_scan", "sqlite_scan",
    "postgres_scan", "mysql_scan", "getenv", "current_setting", "which_secret", "load_extension",
    "checkpoint", "force_checkpoint", "import_database", "export_database", "system",
})
DENIED_FUNCTION_PREFIXES = ("read_", "duckdb_", "pragma_", "sqlite_", "postgres_", "mysql_",
                            "test_", "pg_")

# Statement / command node types that must not appear anywhere in the tree. getattr keeps this
# working if a sqlglot version does not define one of them.
_DENIED_NODE_NAMES = ("Insert", "Update", "Delete", "Merge", "Create", "Drop", "Alter",
                      "AlterColumn", "TruncateTable", "Command", "Copy", "Set", "Pragma",
                      "Attach", "Detach", "Install", "Use", "Transaction", "Commit", "Rollback",
                      "Describe", "Show", "Summarize", "Cache", "Uncache", "Refresh", "Grant",
                      "Revoke", "Comment", "Analyze", "LoadData", "Export")
DENIED_NODES = tuple(t for t in (getattr(exp, n, None) for n in _DENIED_NODE_NAMES) if t)


class SqlRejected(ValueError):
    """The SQL was refused before running; `str(err)` says why."""


class SqlExecutionError(RuntimeError):
    """The SQL passed the checks but DuckDB failed on it (unknown column, type error...);
    `str(err)` is DuckDB's message, so the caller can fix the query."""


class SqlTimeout(SqlExecutionError):
    """The query ran longer than the timeout and was stopped."""


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[tuple]
    row_count: int
    sql_executed: str           # the exact SQL that ran (after the LIMIT rewrite)
    limit_applied: bool         # True if the tool added or lowered the LIMIT
    truncated: bool             # the row cap was reached: more rows may exist
    elapsed_ms: float
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        """JSON-friendly copy (exact decimals become strings, so nothing is rounded)."""
        def cell(v):
            return str(v) if isinstance(v, Decimal) else v
        return {"columns": self.columns, "rows": [[cell(v) for v in r] for r in self.rows],
                "row_count": self.row_count, "sql_executed": self.sql_executed,
                "limit_applied": self.limit_applied, "truncated": self.truncated,
                "elapsed_ms": self.elapsed_ms, "notes": self.notes}


# ---------------------------------------------------------------------------- layer 1: checks
def _function_name(node: exp.Func) -> str:
    if isinstance(node, exp.Anonymous):
        return str(node.name).lower()
    return node.sql_name().lower()


def _function_denied(name: str) -> bool:
    return name in DENIED_FUNCTIONS or name.startswith(DENIED_FUNCTION_PREFIXES)


def _parse_one(sql: str) -> exp.Expression:
    if not isinstance(sql, str) or not sql.strip():
        raise SqlRejected("empty query")
    try:
        statements = sqlglot.parse(sql, read="duckdb")
    except sqlglot.errors.SqlglotError as e:
        raise SqlRejected(f"could not parse the SQL: {str(e).splitlines()[0]}") from None
    # A trailing ';' (or ';' followed by a comment) gives an empty statement: ignore those.
    statements = [s for s in statements if s is not None and not isinstance(s, exp.Semicolon)]
    if len(statements) != 1:
        raise SqlRejected(f"exactly one statement is allowed, got {len(statements)}")
    return statements[0]


def check_sql(sql: str, max_rows: int = MAX_ROWS) -> tuple[str, bool]:
    """Check one query and return (SQL to run, whether the LIMIT was added or lowered).
    Raises SqlRejected with the reason if anything is not allowed."""
    root = _parse_one(sql)
    if not isinstance(root, exp.Query):
        raise SqlRejected(f"only SELECT queries are allowed, got {type(root).__name__.upper()}")

    for node in root.walk():
        if isinstance(node, DENIED_NODES):
            raise SqlRejected(f"{type(node).__name__.upper()} is not allowed inside a query")

    cte_names = {str(c.alias_or_name).lower() for c in root.find_all(exp.CTE)}
    for table in root.find_all(exp.Table):
        if not isinstance(table.this, exp.Identifier):
            raise SqlRejected("table functions are not allowed in FROM "
                              f"({table.this.sql('duckdb')[:60]}); use the gold tables")
        if table.args.get("db") or table.args.get("catalog"):
            raise SqlRejected(f"qualified table names are not allowed ({table.sql('duckdb')}); "
                              "write the bare table name")
        name = str(table.name).lower()
        if name not in ALLOWED_TABLES and name not in cte_names:
            raise SqlRejected(f"table '{table.name}' is not allowed; allowed tables: "
                              + ", ".join(sorted(ALLOWED_TABLES)))

    for func in root.find_all(exp.Func):
        name = _function_name(func)
        if _function_denied(name):
            raise SqlRejected(f"function {name}() is not allowed")

    for ident in root.find_all(exp.Identifier):
        if str(ident.name).lower() in HIDDEN_COLUMNS:
            raise SqlRejected(f"column '{ident.name}' (the school code) is hidden; "
                              "use school_key to join school-level tables")

    limited, limit_applied = _apply_limit(root, max_rows)
    return limited.sql(dialect="duckdb"), limit_applied


# Tables with one row per school (or finer). Their rows must not leave the tool un-aggregated
# when the result is sent to an outside LLM (phase 4): UDISE+ terms forbid redistributing
# school-level data. Marts and the small dimensions are aggregate or reference data already.
SCHOOL_LEVEL_TABLES = frozenset({"int_school_year", "fact_enrolment", "fact_teachers",
                                 "fact_school_facilities", "dim_school"})
SCHOOL_GRAIN_COLUMNS = frozenset({"school_key"})


def _is_aggregating(select: exp.Select) -> bool:
    """GROUP BY, or an aggregate function in the select list that is not a window function
    (count(*) over () keeps one row per school)."""
    if select.args.get("group"):
        return True
    for projection in select.expressions:
        for agg in projection.find_all(exp.AggFunc):
            if not agg.find_ancestor(exp.Window):
                return True
    return False


def _query_level(query: exp.Expression, ctes: dict[str, exp.Expression], depth: int = 0) -> str:
    """'school' if the rows of this query can be school-level rows, else 'agg'."""
    if depth > 50:
        return "school"
    if isinstance(query, exp.Subquery):
        return _query_level(query.this, ctes, depth + 1)
    if isinstance(query, exp.SetOperation):   # UNION / INTERSECT / EXCEPT: any school branch
        sides = (query.left, query.right)
        return "school" if any(_query_level(s, ctes, depth + 1) == "school" for s in sides) else "agg"
    if not isinstance(query, exp.Select):
        return "agg"
    local = dict(ctes)
    with_ = query.args.get("with_") or query.args.get("with")
    for cte in (with_.expressions if with_ else []):
        local[str(cte.alias_or_name).lower()] = cte.this
    if _is_aggregating(query):
        group = query.args.get("group")
        if group and any(isinstance(c, exp.Column) and str(c.name).lower() in SCHOOL_GRAIN_COLUMNS
                         for c in group.find_all(exp.Column)):
            return "school"          # group by school_key = one row per school
        return "agg"
    sources = []
    from_ = query.args.get("from_") or query.args.get("from")
    if from_:
        sources.append(from_.this)
    sources += [j.this for j in query.args.get("joins") or []]
    for src in sources:
        if isinstance(src, exp.Table):
            name = str(src.name).lower()
            if name in local:
                if _query_level(local[name], {k: v for k, v in local.items() if k != name},
                                depth + 1) == "school":
                    return "school"
            elif name in SCHOOL_LEVEL_TABLES:
                return "school"
        elif isinstance(src, exp.Subquery) and _query_level(src, local, depth + 1) == "school":
            return "school"
    return "agg"


def check_aggregated(sql: str) -> None:
    """Raise SqlRejected if the query can return one row per school: a query that reads a
    school-level table must aggregate it (GROUP BY or count/sum/avg...), not by school_key.
    A heuristic on top of check_sql, used when results are sent to an outside LLM."""
    root = _parse_one(sql)
    if _query_level(root, {}) == "school":
        raise SqlRejected("the query would return school-level rows; aggregate with GROUP BY and "
                          "count/sum/avg (school-level data must not leave the database)")


def _apply_limit(root: exp.Query, max_rows: int) -> tuple[exp.Query, bool]:
    """Keep a plain LIMIT n with n <= max_rows; otherwise (missing, larger, a percentage, an
    expression, FETCH FIRST ...) set LIMIT max_rows. OFFSET is kept."""
    limit, fetch = root.args.get("limit"), root.args.get("fetch")
    if fetch is None and isinstance(limit, exp.Limit):
        value, options = limit.expression, limit.args.get("limit_options")
        percent = bool(options and options.args.get("percent"))
        if (isinstance(value, exp.Literal) and not value.is_string and not percent
                and str(value.this).isdigit() and int(value.this) <= max_rows):
            return root, False
    return root.limit(max_rows), True


# ----------------------------------------------------------------------------- the database
def default_gold_db() -> Path:
    """`UDISE_GOLD_DB` if set; else this checkout's data/gold/udise.duckdb if it exists; else the
    main checkout's (a git worktree has no gold database of its own: it is gitignored)."""
    env = os.environ.get("UDISE_GOLD_DB")
    if env:
        return Path(env)
    local = ROOT / "data" / "gold" / "udise.duckdb"
    if local.exists():
        return local
    try:
        common = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
                                cwd=ROOT, capture_output=True, text=True, timeout=10).stdout.strip()
        if common:
            return Path(common).parent / "data" / "gold" / "udise.duckdb"
    except (OSError, subprocess.SubprocessError):
        pass
    return local


class SqlTool:
    """Read-only access to the gold DuckDB through `run(sql)`. Use as a context manager, or call
    `close()`. One query at a time per tool."""

    def __init__(self, gold_db: Path | str | None = None, timeout_s: float = DEFAULT_TIMEOUT_S,
                 max_rows: int = MAX_ROWS):
        self.gold_db = Path(gold_db) if gold_db else default_gold_db()
        if not self.gold_db.exists():
            raise FileNotFoundError(f"gold database not found: {self.gold_db} "
                                    "(run `python run_pipeline.py`, or set UDISE_GOLD_DB)")
        self.timeout_s, self.max_rows = timeout_s, max_rows
        self.con = duckdb.connect(str(self.gold_db), read_only=True, config={
            "enable_external_access": False, "memory_limit": MEMORY_LIMIT, "threads": THREADS})
        catalog = self.con.execute("select current_database()").fetchone()[0]
        missing = ALLOWED_TABLES - {r[0] for r in self.con.execute(
            "select table_name from information_schema.tables where table_schema = 'main'").fetchall()}
        if missing:
            self.con.close()
            raise RuntimeError(f"gold database {self.gold_db} lacks tables: {sorted(missing)}")
        # Temporary views shadow the tables that hold the school code (temp objects are looked
        # up first), so `pseudocode` cannot be selected even by `select *`.
        for table in sorted(ALLOWED_TABLES):
            cols = {r[0] for r in self.con.execute(
                "select column_name from information_schema.columns "
                "where table_schema = 'main' and table_name = ?", [table]).fetchall()}
            hidden = cols & HIDDEN_COLUMNS
            if hidden:
                self.con.execute(f'create temp view "{table}" as select * exclude ({", ".join(sorted(hidden))}) '
                                 f'from "{catalog}".main."{table}"')
        self.con.execute("set lock_configuration = true")

    def run(self, sql: str, require_aggregate: bool = False) -> QueryResult:
        """Check and run one query. `require_aggregate=True` (the agent) also refuses queries
        that could return school-level rows (`check_aggregated`)."""
        sql_executed, limit_applied = check_sql(sql, self.max_rows)
        if require_aggregate:
            check_aggregated(sql_executed)
        timer = threading.Timer(self.timeout_s, self.con.interrupt)
        t0 = time.perf_counter()
        timer.start()
        try:
            cur = self.con.execute(sql_executed)
            rows = cur.fetchall()
            columns = [d[0] for d in cur.description]
        except duckdb.InterruptException:
            raise SqlTimeout(f"query stopped after the {self.timeout_s:g} s timeout") from None
        except duckdb.Error as e:
            raise SqlExecutionError(f"{type(e).__name__}: {e}") from None
        finally:
            timer.cancel()
        elapsed = round((time.perf_counter() - t0) * 1000, 1)
        truncated = len(rows) >= self.max_rows
        notes = []
        if limit_applied:
            notes.append(f"LIMIT {self.max_rows} was applied by the tool")
        if truncated:
            notes.append(f"the {self.max_rows}-row cap was reached: more rows may exist; aggregate instead")
        return QueryResult(columns, rows, len(rows), sql_executed, limit_applied, truncated, elapsed, notes)

    def close(self) -> None:
        self.con.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
