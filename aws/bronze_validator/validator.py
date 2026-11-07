"""Bronze file contract checks. Pure Python + pyarrow: no AWS calls, so tests run locally.

A bronze file is a faithful Parquet copy of one raw zip (pipeline/bronze.py). The checks
below are about the FILE, not its rows: is it the file we expect, intact and complete?

    not_in_contract       the S3 key follows bronze/<dataset>/year=<YYYY-YY>/<file>.parquet and the
                          file is listed in contract.json (made by python -m aws.make_contract)
    unreadable_parquet    the bytes are a readable Parquet file
    schema_mismatch       exactly the expected columns, in order, with the expected types
    row_count_mismatch    exactly the expected number of rows
    year_mismatch         every row's _year equals the year=<YYYY-YY> folder
    source_row_sequence   _source_row runs 1, 2, ..., n in file order (no row lost or repeated)

Row-level quality rules (missing keys, non-numbers, negative counts, unknown districts,
duplicates across files, ...) are NOT checked here: they belong to silver
(pipeline/rules.py, run in Athena by aws/athena_build.py), exactly as in the local pipeline.

validate() returns every failure found (an empty list = the file passes). As in silver, the
file's _rule is the first one it fails; the later ones are listed for information.
"""
import io
import json
import re
from pathlib import Path

import pyarrow.parquet as pq

CONTRACT_PATH = Path(__file__).with_name("contract.json")
KEY_PATTERN = re.compile(r"^bronze/(?P<dataset>[a-z0-9]+)/year=(?P<year>\d{4}-\d{2})/(?P<name>[^/]+\.parquet)$")
RULES = ["not_in_contract", "unreadable_parquet", "schema_mismatch", "row_count_mismatch",
         "year_mismatch", "source_row_sequence"]


def load_contract(path: Path = CONTRACT_PATH) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def failure(rule: str, reason: str, expected=None, actual=None) -> dict:
    return {"rule": rule, "reason": reason, "expected": expected, "actual": actual}


def schema_differences(expected: list[list[str]], actual: list[list[str]]) -> str:
    """Short description of how two column lists differ."""
    exp, act = dict(map(tuple, expected)), dict(map(tuple, actual))
    parts = []
    missing = [c for c in exp if c not in act]
    extra = [c for c in act if c not in exp]
    changed = [f"{c} {exp[c]} -> {act[c]}" for c in exp if c in act and exp[c] != act[c]]
    if missing:
        parts.append(f"missing columns: {', '.join(missing)}")
    if extra:
        parts.append(f"unexpected columns: {', '.join(extra)}")
    if changed:
        parts.append(f"type changed: {'; '.join(changed)}")
    if not parts:
        parts.append("same columns in a different order")
    return "; ".join(parts)


def validate(key: str, data: bytes, contract: dict) -> list[dict]:
    """All contract failures of one uploaded bronze file (empty list = passes)."""
    match = KEY_PATTERN.match(key)
    if not match:
        return [failure("not_in_contract", "key does not follow bronze/<dataset>/year=<YYYY-YY>/<file>.parquet",
                        KEY_PATTERN.pattern, key)]
    entry = contract["files"].get(key)
    if entry is None:
        return [failure("not_in_contract", "file is not in contract.json (new data: regenerate it with "
                        "python -m aws.make_contract and redeploy)", None, key)]

    try:
        pf = pq.ParquetFile(io.BytesIO(data))
        actual_schema = [[f.name, str(f.type)] for f in pf.schema_arrow]
        rows = pf.metadata.num_rows
    except Exception as e:  # any parse error means the file is not usable
        return [failure("unreadable_parquet", f"not a readable Parquet file: {type(e).__name__}: {e}",
                        None, f"{len(data)} bytes")]

    fails = []
    expected_schema = contract["schemas"][entry["schema"]]
    if actual_schema != expected_schema:
        fails.append(failure("schema_mismatch", schema_differences(expected_schema, actual_schema),
                             expected_schema, actual_schema))
    if rows != entry["rows"]:
        fails.append(failure("row_count_mismatch", f"{rows:,} rows, expected {entry['rows']:,}",
                             entry["rows"], rows))

    present = set(pf.schema_arrow.names)
    columns = [c for c in ("_year", "_source_row") if c in present]
    table = pf.read(columns=columns) if columns else None
    if "_year" in present:
        years = sorted({y for y in table["_year"].to_pylist()}, key=str)
        if years != [match["year"]]:
            fails.append(failure("year_mismatch", f"_year values {years} but the folder is year={match['year']}",
                                 match["year"], years))
    if "_source_row" in present:
        seq = table["_source_row"].to_pylist()
        if seq != list(range(1, len(seq) + 1)):
            first_bad = next(i for i, v in enumerate(seq, 1) if v != i)
            fails.append(failure("source_row_sequence",
                                 f"_source_row should run 1..{len(seq)} in file order; row {first_bad} has "
                                 f"{seq[first_bad - 1]}", "1..n", f"row {first_bad} = {seq[first_bad - 1]}"))
    return fails
