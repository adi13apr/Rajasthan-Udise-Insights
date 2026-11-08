"""Tests for the Athena build (aws/athena.py, aws/athena_build.py) without AWS: the S3 prefix
guard, and that every SQL file in sql/athena/ parses into blocks whose placeholders are filled."""
import re

import pytest

from aws import athena_build as B
from aws.athena import CLEARABLE, Athena


class NoS3:
    """Fails the test if the code would list (and so delete from) S3."""
    def get_paginator(self, name):
        raise AssertionError("S3 would have been listed")


def athena_without_aws():
    a = Athena.__new__(Athena)
    a.bucket, a.s3 = "udise-lake-aditya-2026", NoS3()
    return a


@pytest.mark.parametrize("prefix", [
    "bronze/", "bronze/facility/", "manual-test/", "athena-results/", "quarantine/", "quarantine/files/",
    "artifacts/", "", "silver", "silver/", "gold/", "gold/../bronze/", "gold/dim_school/../../bronze/",
    "silver/schools", "silver/schools/year=2024-25/", "Silver/schools/"])
def test_clear_prefix_refuses_anything_it_does_not_own(prefix):
    with pytest.raises(ValueError):
        athena_without_aws().clear_prefix(prefix)


@pytest.mark.parametrize("prefix", ["silver/schools/", "silver/enrolment_other/", "quarantine/rows/teachers/",
                                    "gold/dim_school/", "reference/district_aliases/"])
def test_clear_prefix_allows_one_table_folder_of_its_own(prefix):
    with pytest.raises(AssertionError, match="would have been listed"):
        athena_without_aws().clear_prefix(prefix)


def test_every_prefix_the_build_writes_is_clearable():
    """The prefixes used by the build (one folder per table) all pass the guard; a table folder
    is never a prefix of another (silver/enrolment/ vs silver/enrolment_other/)."""
    prefixes = ([f"silver/{t}/" for t in B.SILVER_TABLES] + [f"quarantine/rows/{t}/" for t in B.SILVER_TABLES]
                + [f"reference/{n}/" for n in ("districts", "district_aliases", "management")]
                + [f"gold/{name}/" for p in sorted(B.SQL_DIR.glob("*.sql"))
                   for kind, name, _, _ in B.sql_blocks(p.read_text(encoding="utf-8")) if kind == "table"])
    for p in prefixes:
        assert p.startswith(CLEARABLE) and re.fullmatch(r"[a-z_/]+/", p)
    assert not [(a, b) for a in prefixes for b in prefixes if a != b and b.startswith(a)]


def test_workgroup_allows_ctas_locations_and_keeps_the_scan_limit():
    """An enforcing workgroup rejects CTAS with external_location (the first build failed on that),
    and the build uses external_location for every table. The 1 GB per-query limit must stay."""
    text = (B.C.ROOT / "aws" / "template.yaml").read_text(encoding="utf-8")
    assert re.search(r"^\s+EnforceWorkGroupConfiguration: false\b", text, re.M)
    assert re.search(r"^\s+BytesScannedCutoffPerQuery: 1073741824\b", text, re.M)
    import inspect
    from aws.athena import Athena as A
    assert "external_location" in inspect.getsource(A.ctas)


def test_sql_files_parse_and_placeholders_are_known():
    names = []
    for path in sorted(B.SQL_DIR.glob("*.sql")) + sorted((B.SQL_DIR / "tests").glob("*.sql")):
        text = path.read_text(encoding="utf-8")
        assert set(re.findall(r"\{\{(\w+)\}\}", text)) <= {"enrolment_column_names", "enrolment_columns",
                                                            "enrolment_row_sum", "teacher_columns"}
        names += [n for _, n, _, _ in B.sql_blocks(text)]
    from spark.parity import GOLD
    assert set(GOLD) <= set(names)   # every gold table the parity check compares is built
