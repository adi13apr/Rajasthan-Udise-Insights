"""Tests for the AWS layer's bronze validator Lambda and the bucket-settings custom resource.

Synthetic data only, and no AWS calls: bronze files are made by the real pipeline code
(pipeline.bronze.load_bronze) from tiny zips, the contract by aws.make_contract, and S3 is a
small fake, and so is the DynamoDB manifest table. boto3 is not needed (the main CI job does
not install it).
"""
import hashlib
import importlib
import io
import json
import re
import sys
import types
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from aws.bronze_validator import validator as V
from aws.make_contract import METADATA_COLUMNS, build_contract
from helpers import make_zip
from pipeline.bronze import load_bronze

ROOT = Path(__file__).resolve().parents[1]
PROFILE = "pseudocode,district,managment\n0001,KOTA,1\n0002,KOTA,9\n0003,KOTA,NA\n"
ENROL = "pseudocode,item_group,item_id,c1_b\n0001,1,1,5\n0001,1,2,7\n"
KEY = "bronze/profile1/year=2024-25/profile_data_1_KOTA_2024-25.parquet"
ODD_KEY = "bronze/enrolment1/year=2025-26/enrolment_data_1_PRATAPGARH (RAJ.)_2025-26.parquet"


@pytest.fixture
def bronze(tmp_path):
    raw = tmp_path / "raw"
    make_zip(raw / "2024-25" / "profile_data_1_KOTA_2024-25.zip", PROFILE)
    make_zip(raw / "2025-26" / "enrolment_data_1_PRATAPGARH (RAJ.)_2025-26.zip", ENROL)
    out = tmp_path / "bronze"
    load_bronze(raw, out, run_id="r1")
    return out


@pytest.fixture
def contract(bronze):
    return build_contract(bronze)


def file_bytes(bronze: Path, key: str) -> bytes:
    return (bronze / key[len("bronze/"):]).read_bytes()


def rewrite(data: bytes, change) -> bytes:
    """Read a bronze file, change the Arrow table, write it back as Parquet bytes."""
    table = change(pq.read_table(io.BytesIO(data)))
    buf = io.BytesIO()
    pq.write_table(table, buf)
    return buf.getvalue()


def set_column(table: pa.Table, name: str, values, type_=None) -> pa.Table:
    i = table.schema.get_field_index(name)
    return table.set_column(i, name, pa.array(values, type=type_ or table.schema.field(i).type))


def rules(fails):
    return [f["rule"] for f in fails]


# --- The contract -----------------------------------------------------------------------------

def test_contract_lists_every_bronze_file(contract):
    assert set(contract["files"]) == {KEY, ODD_KEY}
    entry = contract["files"][KEY]
    assert {k: entry[k] for k in ("dataset", "year", "rows")} == {"dataset": "profile1", "year": "2024-25", "rows": 3}
    names = [n for n, _ in contract["schemas"][entry["schema"]]]
    assert names == ["pseudocode", "district", "managment"] + METADATA_COLUMNS


def test_committed_contract_is_well_formed():
    """aws/bronze_validator/contract.json (from the real bronze) has no school data, only files."""
    c = V.load_contract()
    assert c["files"] and all(V.KEY_PATTERN.match(k) for k in c["files"])
    for cols in c["schemas"].values():
        assert [n for n, _ in cols][-6:] == METADATA_COLUMNS
    assert all(set(f) == {"dataset", "year", "rows", "schema"} for f in c["files"].values())


# --- Checks -----------------------------------------------------------------------------------

def test_files_from_the_pipeline_pass(bronze, contract):
    for key in (KEY, ODD_KEY):
        assert V.validate(key, file_bytes(bronze, key), contract) == []


def test_unknown_file_or_bad_key_is_not_in_contract(bronze, contract):
    data = file_bytes(bronze, KEY)
    assert rules(V.validate(KEY.replace("KOTA", "BUNDI"), data, contract)) == ["not_in_contract"]
    assert rules(V.validate("bronze/profile1/profile_data_1_KOTA_2024-25.parquet", data, contract)) \
        == ["not_in_contract"]


def test_unreadable_file(contract):
    fails = V.validate(KEY, b"PK\x03\x04 this is a zip, not parquet", contract)
    assert rules(fails) == ["unreadable_parquet"]


def test_missing_column_is_a_schema_mismatch(bronze, contract):
    data = rewrite(file_bytes(bronze, KEY), lambda t: t.drop_columns(["managment"]))
    fails = V.validate(KEY, data, contract)
    assert rules(fails) == ["schema_mismatch"]
    assert "missing columns: managment" in fails[0]["reason"]


def test_changed_type_is_a_schema_mismatch(bronze, contract):
    data = rewrite(file_bytes(bronze, KEY),
                   lambda t: set_column(t, "_source_row", ["1", "2", "3"], pa.string()))
    fails = V.validate(KEY, data, contract)
    assert rules(fails)[0] == "schema_mismatch"
    assert "type changed: _source_row int64 -> string" in fails[0]["reason"]


def test_lost_row_is_a_row_count_mismatch(bronze, contract):
    data = rewrite(file_bytes(bronze, KEY), lambda t: t.slice(0, 2))
    fails = V.validate(KEY, data, contract)
    assert rules(fails) == ["row_count_mismatch"]
    assert fails[0]["expected"] == 3 and fails[0]["actual"] == 2


def test_wrong_year_in_rows(bronze, contract):
    data = rewrite(file_bytes(bronze, KEY), lambda t: set_column(t, "_year", ["2024-25", "2025-26", "2024-25"]))
    assert rules(V.validate(KEY, data, contract)) == ["year_mismatch"]


def test_year_check_reads_the_footer_statistics(bronze, contract):
    """min = max = the folder's year in every row group, read from the footer statistics."""
    meta = pq.ParquetFile(io.BytesIO(file_bytes(bronze, KEY))).metadata
    names = [meta.schema.column(j).name for j in range(meta.num_columns)]
    stats = meta.row_group(0).column(names.index("_year")).statistics
    assert (stats.min, stats.max, stats.null_count) == ("2024-25", "2024-25", 0)


def test_empty_year_fails(bronze, contract):
    data = rewrite(file_bytes(bronze, KEY), lambda t: set_column(t, "_year", ["2024-25", None, "2024-25"]))
    fails = V.validate(KEY, data, contract)
    assert rules(fails) == ["year_mismatch"] and "1 empty values" in fails[0]["reason"]


def test_file_without_statistics_fails_the_year_check(bronze, contract):
    """Without min/max statistics the year cannot be checked from the footer: fail, don't guess."""
    table = pq.read_table(io.BytesIO(file_bytes(bronze, KEY)))
    buf = io.BytesIO()
    pq.write_table(table, buf, write_statistics=False)
    fails = V.validate(KEY, buf.getvalue(), contract)
    assert rules(fails) == ["year_mismatch"] and "no min/max statistics" in fails[0]["reason"]


def test_source_row_order_is_not_checked_here(bronze, contract):
    """_source_row needs the data pages, so it is checked in Athena on the whole bronze table
    (sql/athena/tests/00_bronze_source_rows.sql), not by the footer-only validator."""
    data = rewrite(file_bytes(bronze, KEY), lambda t: set_column(t, "_source_row", [1, 1, 3]))
    assert V.validate(KEY, data, contract) == []
    test_sql = (ROOT / "sql" / "athena" / "tests" / "00_bronze_source_rows.sql").read_text(encoding="utf-8")
    assert test_sql.count("n = d and d = hi and lo = 1") == 4   # one per bronze dataset


DATA_READS = ["read", "read_row_group", "read_row_groups", "iter_batches", "scan_contents"]


def test_validator_reads_only_the_footer(bronze, contract, monkeypatch):
    """The Lambda layer's pyarrow cannot decompress zstd data pages (the first deploy failed on
    every file with exactly this error). Any attempt to read data must fail the test."""
    table = set_column(pq.read_table(io.BytesIO(file_bytes(bronze, KEY))), "_year", ["2025-26"] * 3)
    buf = io.BytesIO()
    pq.write_table(table, buf, compression="zstd")   # zstd, as bronze is; made before reads are blocked
    bad_year, good = buf.getvalue(), file_bytes(bronze, KEY)

    def no_data(*args, **kwargs):
        raise pa.lib.ArrowNotImplementedError("Support for codec 'zstd' not built")
    for name in DATA_READS:
        monkeypatch.setattr(pq.ParquetFile, name, no_data)
    monkeypatch.setattr(pq, "read_table", no_data)

    assert V.validate(KEY, good, contract) == []                              # a good file passes
    assert rules(V.validate(KEY, bad_year, contract)) == ["year_mismatch"]    # a bad one fails


def test_every_failure_is_listed_first_one_is_the_rule(bronze, contract):
    def break_it(t):
        t = t.drop_columns(["district"]).slice(0, 2)
        return set_column(t, "_year", ["2023-24", "2023-24"])
    fails = V.validate(KEY, rewrite(file_bytes(bronze, KEY), break_it), contract)
    assert rules(fails) == ["schema_mismatch", "row_count_mismatch", "year_mismatch"]
    assert rules(fails) == sorted(rules(fails), key=V.RULES.index)


# --- Lambda handler with a fake S3 ------------------------------------------------------------

def etag(data: bytes) -> str:
    """S3's etag of a single-part upload: the MD5 of the bytes."""
    return hashlib.md5(data).hexdigest()


class FakeS3:
    def __init__(self, objects: dict[str, bytes], calls: list | None = None):
        self.objects = dict(objects)
        self.calls = [] if calls is None else calls

    def get_object(self, Bucket, Key):
        self.calls.append(("get", Key))
        data = self.objects[Key]
        return {"Body": io.BytesIO(data), "ETag": f'"{etag(data)}"'}

    def head_object(self, Bucket, Key):
        self.calls.append(("head", Key))
        return {"ETag": f'"{etag(self.objects[Key])}"', "ContentLength": len(self.objects[Key])}

    def put_object(self, Bucket, Key, Body, ContentType=None):
        self.calls.append(("put", Key))
        self.objects[Key] = Body

    def delete_object(self, Bucket, Key):
        self.calls.append(("delete", Key))
        del self.objects[Key]


class FakeTable:
    """The DynamoDB manifest table: get_item / put_item of the boto3 Table resource, plain values."""
    def __init__(self, calls: list | None = None):
        self.items: dict[str, dict] = {}
        self.calls = [] if calls is None else calls

    def get_item(self, Key, ConsistentRead=False):
        self.calls.append(("ddb_get", Key["s3_key"]))
        item = self.items.get(Key["s3_key"])
        return {"Item": dict(item)} if item else {}

    def put_item(self, Item):
        self.calls.append(("ddb_put", Item["s3_key"]))
        self.items[Item["s3_key"]] = dict(Item)


class FakeDynamoResource:
    def Table(self, name):
        return FakeTable()


@pytest.fixture
def handler(monkeypatch, contract):
    """aws/bronze_validator/handler.py imported as in Lambda, with boto3, the contract and the
    manifest table faked (handler.MANIFEST is an empty FakeTable)."""
    folder = ROOT / "aws" / "bronze_validator"
    monkeypatch.syspath_prepend(str(folder))
    fake_boto3 = types.SimpleNamespace(client=lambda name: None, resource=lambda name: FakeDynamoResource())
    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)
    monkeypatch.delitem(sys.modules, "handler", raising=False)
    monkeypatch.delitem(sys.modules, "validator", raising=False)
    module = importlib.import_module("handler")
    monkeypatch.setattr(module, "CONTRACT", contract)
    monkeypatch.setattr(module, "MANIFEST", FakeTable())
    return module


def s3_event(*keys):
    from urllib.parse import quote_plus
    return {"Records": [{"s3": {"bucket": {"name": "b"}, "object": {"key": quote_plus(k, safe="/=")}}}
                        for k in keys]}


def test_handler_keeps_good_files_and_quarantines_bad_ones(handler, bronze, monkeypatch):
    bad = rewrite(file_bytes(bronze, KEY), lambda t: t.slice(0, 1))
    s3 = FakeS3({KEY: bad, ODD_KEY: file_bytes(bronze, ODD_KEY)})
    monkeypatch.setattr(handler, "S3", s3)

    result = handler.lambda_handler(s3_event(KEY, ODD_KEY), types.SimpleNamespace(aws_request_id="req-1"))

    assert (result["checked"], result["quarantined"]) == (2, 1)
    dest = "quarantine/files/profile1/year=2024-25/profile_data_1_KOTA_2024-25.parquet"
    assert KEY not in s3.objects and ODD_KEY in s3.objects          # good file left alone
    assert s3.objects[dest] == bad                                   # bad file moved intact
    reason = json.loads(s3.objects[dest + ".reason.json"])
    assert reason["_rule"] == "row_count_mismatch" and reason["_run_id"] == "req-1"
    assert reason["source_key"] == KEY and reason["failures"][0]["expected"] == 3
    # copy and reason written before the bronze file is deleted: a file is never lost
    order = [c for c in s3.calls if c[1].startswith(("quarantine/", KEY)) and c[0] != "get"]
    assert order == [("put", dest), ("put", dest + ".reason.json"), ("delete", KEY)]


def test_handler_decodes_keys_with_spaces_and_skips_other_prefixes(handler, bronze, monkeypatch):
    s3 = FakeS3({ODD_KEY: file_bytes(bronze, ODD_KEY)})
    monkeypatch.setattr(handler, "S3", s3)
    result = handler.lambda_handler(s3_event(ODD_KEY, "quarantine/files/x.parquet"), None)
    assert (result["checked"], result["quarantined"]) == (1, 0)
    assert [r["result"] for r in result["results"]] == ["ok", "skipped"]
    assert s3.calls == [("get", ODD_KEY)]


def test_direct_call_dry_run_moves_nothing(handler, bronze, monkeypatch):
    """{"keys": [...], "dry_run": true}: the smoke test in aws/deploy.sh."""
    bad = rewrite(file_bytes(bronze, KEY), lambda t: t.slice(0, 1))
    s3 = FakeS3({KEY: bad, ODD_KEY: file_bytes(bronze, ODD_KEY)})
    monkeypatch.setattr(handler, "S3", s3)
    monkeypatch.setattr(handler, "BUCKET", "b")
    result = handler.lambda_handler({"keys": [ODD_KEY, KEY], "dry_run": True}, None)
    assert [r["result"] for r in result["results"]] == ["ok", "would_quarantine"]
    assert result["results"][1]["rule"] == "row_count_mismatch"
    assert (result["quarantined"], result["would_quarantine"]) == (0, 1)
    assert all(c[0] == "get" for c in s3.calls) and set(s3.objects) == {KEY, ODD_KEY}


def test_direct_call_quarantines_like_an_upload(handler, bronze, monkeypatch):
    """{"keys": [...]}: aws/revalidate_bronze.py, for files already in bronze/."""
    bad = rewrite(file_bytes(bronze, KEY), lambda t: t.slice(0, 1))
    s3 = FakeS3({KEY: bad})
    monkeypatch.setattr(handler, "S3", s3)
    monkeypatch.setattr(handler, "BUCKET", "b")
    result = handler.lambda_handler({"keys": [KEY]}, types.SimpleNamespace(aws_request_id="req-2"))
    assert result["quarantined"] == 1 and KEY not in s3.objects
    assert json.loads(s3.objects[result["results"][0]["quarantine_key"] + ".reason.json"])["_run_id"] == "req-2"


# --- Ingestion manifest (DynamoDB) with a fake table --------------------------------------------

ITEM_FIELDS = {"s3_key", "dataset", "year", "row_count", "etag", "status", "reason", "validated_at", "run_id"}


def test_manifest_item_written_when_a_file_passes(handler, bronze, monkeypatch):
    data = file_bytes(bronze, ODD_KEY)
    monkeypatch.setattr(handler, "S3", FakeS3({ODD_KEY: data}))
    result = handler.lambda_handler(s3_event(ODD_KEY), types.SimpleNamespace(aws_request_id="req-3"))
    assert result["results"][0]["result"] == "ok"
    item = handler.MANIFEST.items[ODD_KEY]
    assert set(item) == ITEM_FIELDS
    assert (item["dataset"], item["year"], item["row_count"]) == ("enrolment1", "2025-26", 2)
    assert (item["status"], item["etag"], item["run_id"]) == ("PASSED", etag(data), "req-3")
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00", item["validated_at"])


def test_manifest_item_written_when_a_file_is_quarantined(handler, bronze, monkeypatch):
    bad = rewrite(file_bytes(bronze, KEY), lambda t: t.slice(0, 1))
    calls = []                                       # one log for S3 and DynamoDB: the order of writes
    monkeypatch.setattr(handler, "S3", FakeS3({KEY: bad}, calls))
    monkeypatch.setattr(handler, "MANIFEST", FakeTable(calls))
    result = handler.lambda_handler(s3_event(KEY), types.SimpleNamespace(aws_request_id="req-4"))
    assert result["quarantined"] == 1
    item = handler.MANIFEST.items[KEY]
    assert set(item) == ITEM_FIELDS | {"quarantine_key"}
    assert (item["status"], item["etag"], item["row_count"]) == ("QUARANTINED", etag(bad), 1)  # rows in the file
    assert item["reason"] == "row_count_mismatch: 1 rows, expected 3"
    assert item["quarantine_key"] == result["results"][0]["quarantine_key"]
    # the manifest item is written before the bronze file is deleted (a retry finds the file again)
    assert [c[0] for c in calls if c[0] in ("ddb_put", "delete")] == ["ddb_put", "delete"]


def test_same_etag_already_passed_is_skipped(handler, bronze, monkeypatch):
    s3 = FakeS3({ODD_KEY: file_bytes(bronze, ODD_KEY)})
    monkeypatch.setattr(handler, "S3", s3)
    handler.lambda_handler(s3_event(ODD_KEY), None)
    first = dict(handler.MANIFEST.items[ODD_KEY])
    s3.calls.clear()

    result = handler.lambda_handler(s3_event(ODD_KEY), None)          # identical re-upload
    assert result["results"][0]["result"] == "unchanged"
    assert (result["checked"], result["unchanged"]) == (0, 1)
    assert s3.calls == [("head", ODD_KEY)]                            # not downloaded, not checked
    assert handler.MANIFEST.items[ODD_KEY] == first                   # item not rewritten


def test_changed_etag_is_validated_again(handler, bronze, monkeypatch):
    good = file_bytes(bronze, KEY)
    s3 = FakeS3({KEY: good})
    monkeypatch.setattr(handler, "S3", s3)
    handler.lambda_handler(s3_event(KEY), None)
    assert handler.MANIFEST.items[KEY]["status"] == "PASSED"

    bad = rewrite(good, lambda t: t.slice(0, 1))                      # new upload, same key
    s3.objects[KEY] = bad
    result = handler.lambda_handler(s3_event(KEY), None)
    assert result["results"][0]["result"] == "quarantined"
    assert result["results"][0]["previous_status"] == "PASSED"
    item = handler.MANIFEST.items[KEY]
    assert (item["status"], item["etag"]) == ("QUARANTINED", etag(bad))


def test_quarantined_file_is_checked_again_even_with_the_same_etag(handler, bronze, monkeypatch):
    """Only a PASS lets a re-upload skip: the same bad bytes uploaded again are quarantined again."""
    bad = rewrite(file_bytes(bronze, KEY), lambda t: t.slice(0, 1))
    s3 = FakeS3({KEY: bad})
    monkeypatch.setattr(handler, "S3", s3)
    handler.lambda_handler(s3_event(KEY), None)
    s3.objects[KEY] = bad
    result = handler.lambda_handler(s3_event(KEY), None)
    assert result["results"][0]["result"] == "quarantined"
    assert handler.MANIFEST.items[KEY]["status"] == "QUARANTINED"


def test_force_rechecks_a_passed_file(handler, bronze, monkeypatch):
    monkeypatch.setattr(handler, "S3", FakeS3({ODD_KEY: file_bytes(bronze, ODD_KEY)}))
    monkeypatch.setattr(handler, "BUCKET", "b")
    handler.lambda_handler({"keys": [ODD_KEY]}, types.SimpleNamespace(aws_request_id="req-5"))
    result = handler.lambda_handler({"keys": [ODD_KEY], "force": True}, types.SimpleNamespace(aws_request_id="req-6"))
    assert result["results"][0]["result"] == "ok"
    assert handler.MANIFEST.items[ODD_KEY]["run_id"] == "req-6"


def test_dry_run_never_skips_and_writes_nothing(handler, bronze, monkeypatch):
    """The deploy smoke test must exercise the real checks, even on a file that already PASSED."""
    monkeypatch.setattr(handler, "S3", FakeS3({ODD_KEY: file_bytes(bronze, ODD_KEY)}))
    monkeypatch.setattr(handler, "BUCKET", "b")
    handler.lambda_handler({"keys": [ODD_KEY]}, None)
    before = dict(handler.MANIFEST.items)
    handler.MANIFEST.calls.clear()
    result = handler.lambda_handler({"keys": [ODD_KEY], "dry_run": True}, None)
    assert result["results"][0]["result"] == "ok"
    assert result["results"][0]["previous_status"] == "PASSED"
    assert [c[0] for c in handler.MANIFEST.calls] == ["ddb_get"]      # read only
    assert handler.MANIFEST.items == before


def template_block(name: str) -> str:
    """One top-level resource of aws/template.yaml, as text (the main CI job has no PyYAML)."""
    text = (ROOT / "aws" / "template.yaml").read_text(encoding="utf-8")
    return re.search(rf"^  {name}:\n(.*?)(?=^  \S|^\S)", text, re.S | re.M).group(1)


def test_template_manifest_table_and_least_privilege_access():
    table = template_block("ManifestTable")
    assert "Type: AWS::DynamoDB::Table" in table and "TableName: udise-ingest-manifest" in table
    assert "BillingMode: PAY_PER_REQUEST" in table
    assert "{AttributeName: s3_key, AttributeType: S}" in table and "{AttributeName: s3_key, KeyType: HASH}" in table
    role = template_block("ValidatorRole")
    statement = re.search(r"Sid: ReadAndWriteManifestItems\n(.*?)(?=\n\s+- Sid:)", role, re.S).group(1)
    assert "Action: [dynamodb:GetItem, dynamodb:PutItem]" in statement
    assert "Resource: !GetAtt ManifestTable.Arn" in statement
    assert role.count("dynamodb:") == 2                                # nothing else on DynamoDB
    assert "MANIFEST_TABLE: !Ref ManifestTable" in template_block("ValidatorFunction")


# --- Custom resource in aws/template.yaml (bucket trigger + lifecycle rule) -------------------

class ClientError(Exception):  # stands in for botocore.exceptions.ClientError
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeBucketS3:
    def __init__(self, notification=None, rules=None):
        self.notification = notification or {}
        self.rules = rules
        self.deleted_lifecycle = False

    def get_bucket_notification_configuration(self, Bucket):
        return {**json.loads(json.dumps(self.notification)), "ResponseMetadata": {}}

    def put_bucket_notification_configuration(self, Bucket, NotificationConfiguration):
        assert "ResponseMetadata" not in NotificationConfiguration
        self.notification = NotificationConfiguration

    def get_bucket_lifecycle_configuration(self, Bucket):
        if self.rules is None:
            raise ClientError("NoSuchLifecycleConfiguration")
        return {"Rules": list(self.rules)}

    def put_bucket_lifecycle_configuration(self, Bucket, LifecycleConfiguration):
        self.rules = LifecycleConfiguration["Rules"]

    def delete_bucket_lifecycle(self, Bucket):
        self.rules, self.deleted_lifecycle = None, True


def bucket_config_code() -> str:
    """The inline Python of BucketConfigFunction, exactly as CloudFormation saves it as index.py."""
    text = (ROOT / "aws" / "template.yaml").read_text(encoding="utf-8")
    block = re.search(r"ZipFile: \|\n(.*?)\n\n  BucketConfig:", text, re.S).group(1)
    return "\n".join(line[10:] for line in block.splitlines()) + "\n"


# --- Import each Lambda exactly as it is packaged -----------------------------------------------
# Why the first deploy hung: the custom-resource test injected a fake `cfnresponse` module, so
# the code imported fine in the test but not in Lambda (the python3.13 runtime has no such
# module). These tests import the packaged code in a fresh, isolated Python (-I -S: no
# site-packages, no PYTHONPATH, so nothing installed on this machine can hide a missing
# module). The only extra modules available are stand-ins for what Lambda really provides:
# boto3/botocore (runtime) and, for the validator, pyarrow (AWS SDK for pandas layer).

RUNTIME = {"boto3/__init__.py": ("def client(*args, **kwargs):\n    return None\n"
                                  "class _Resource:\n    def Table(self, name):\n        return None\n"
                                  "def resource(*args, **kwargs):\n    return _Resource()\n"),
           "botocore/__init__.py": "",
           "botocore/exceptions.py": "class ClientError(Exception):\n    pass\n"}
PANDAS_LAYER = {"pyarrow/__init__.py": "", "pyarrow/parquet.py": ""}


def import_as_packaged(tmp_path, package: dict, module: str, provided: dict):
    import subprocess
    pkg, stubs = tmp_path / "package", tmp_path / "provided"
    for folder, files in ((pkg, package), (stubs, provided)):
        for name, content in files.items():
            (folder / name).parent.mkdir(parents=True, exist_ok=True)
            (folder / name).write_bytes(content if isinstance(content, bytes) else content.encode())
    code = (f"import sys; sys.path[:0] = [{str(pkg)!r}, {str(stubs)!r}]; "
            f"import {module}; print('imported', {module}.__name__)")
    return subprocess.run([sys.executable, "-I", "-S", "-c", code], capture_output=True, text=True, timeout=60)


def test_bucket_config_helper_imports_as_packaged(tmp_path):
    run = import_as_packaged(tmp_path, {"index.py": bucket_config_code()}, "index", RUNTIME)
    assert run.returncode == 0, run.stderr


def test_import_check_catches_a_module_lambda_does_not_have(tmp_path):
    """The check itself: the first version's `import boto3, cfnresponse` fails it."""
    run = import_as_packaged(tmp_path, {"index.py": "import json\nimport boto3, cfnresponse\n"}, "index", RUNTIME)
    assert run.returncode != 0 and "No module named 'cfnresponse'" in run.stderr


def test_validator_lambda_imports_as_packaged(tmp_path):
    """What `aws cloudformation package` zips from aws/bronze_validator/ (no __pycache__)."""
    folder = ROOT / "aws" / "bronze_validator"
    files = {f.name: f.read_bytes() for f in folder.iterdir() if f.is_file()}
    assert {"handler.py", "validator.py", "contract.json"} <= set(files)
    run = import_as_packaged(tmp_path, files, "handler", {**RUNTIME, **PANDAS_LAYER})
    assert run.returncode == 0, run.stderr


# --- Behaviour of the custom resource ------------------------------------------------------------

@pytest.fixture
def bucket_config(monkeypatch):
    """index.py run in-process with a fake boto3; the reply to CloudFormation (an HTTP PUT to
    ResponseURL) is captured instead of sent. Set ns["s3"] to the fake bucket and
    ns["reply_error"] to make the reply itself fail."""
    ns: dict = {"sent": [], "s3": None, "reply_error": None}

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def urlopen(req, timeout=None):
        if ns["reply_error"]:
            raise ns["reply_error"]
        ns["sent"].append((req.get_method(), req.full_url, json.loads(req.data)))
        return Response()

    monkeypatch.setitem(sys.modules, "boto3", types.SimpleNamespace(client=lambda name: ns["s3"]))
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    exec(compile(bucket_config_code(), "index.py", "exec"), ns)
    return ns


PROPS = {"BucketName": "b", "FunctionArn": "arn:fn", "TriggerPrefix": "bronze/", "TriggerSuffix": ".parquet",
         "ExpirePrefix": "athena-results/", "ExpireDays": "7"}
OTHER_TRIGGER = {"Id": "someone-else", "LambdaFunctionArn": "arn:other", "Events": ["s3:ObjectRemoved:*"]}
OTHER_RULE = {"ID": "keep-me", "Status": "Enabled", "Filter": {"Prefix": "x/"}, "Expiration": {"Days": 30}}


def cfn_event(request_type, **changes):
    event = {"RequestType": request_type, "ResourceProperties": PROPS, "ResponseURL": "https://cfn.example/reply",
             "StackId": "stack-1", "RequestId": "req-1", "LogicalResourceId": "BucketConfig"}
    return {**event, **changes}


def statuses(ns):
    return [body["Status"] for _, _, body in ns["sent"]]


def test_custom_resource_adds_its_settings_and_keeps_others(bucket_config):
    s3 = FakeBucketS3({"LambdaFunctionConfigurations": [OTHER_TRIGGER], "EventBridgeConfiguration": {}},
                      [OTHER_RULE])
    bucket_config["s3"] = s3
    bucket_config["handler"](cfn_event("Create"), None)

    method, url, body = bucket_config["sent"][-1]
    assert (method, url, body["Status"]) == ("PUT", "https://cfn.example/reply", "SUCCESS")
    assert body["RequestId"] == "req-1" and body["PhysicalResourceId"] == "udise-bucket-config-b"
    fns = s3.notification["LambdaFunctionConfigurations"]
    assert fns[0] == OTHER_TRIGGER and "EventBridgeConfiguration" in s3.notification
    ours = fns[1]
    assert ours["Id"] == "udise-bronze-validator" and ours["Events"] == ["s3:ObjectCreated:*"]
    assert ours["Filter"]["Key"]["FilterRules"] == [{"Name": "prefix", "Value": "bronze/"},
                                                   {"Name": "suffix", "Value": ".parquet"}]
    assert s3.rules == [OTHER_RULE, {"ID": "udise-athena-results-expiry", "Status": "Enabled",
                                     "Filter": {"Prefix": "athena-results/"}, "Expiration": {"Days": 7}}]

    # An update replaces its own entries instead of adding a second copy.
    bucket_config["handler"](cfn_event("Update"), None)
    assert len(s3.notification["LambdaFunctionConfigurations"]) == 2 and len(s3.rules) == 2

    # Delete removes only its own entries.
    bucket_config["handler"](cfn_event("Delete", PhysicalResourceId="udise-bucket-config-b"), None)
    assert s3.notification["LambdaFunctionConfigurations"] == [OTHER_TRIGGER]
    assert s3.rules == [OTHER_RULE] and not s3.deleted_lifecycle
    assert statuses(bucket_config) == ["SUCCESS"] * 3


def test_custom_resource_on_an_empty_bucket(bucket_config):
    s3 = FakeBucketS3()  # no trigger, no lifecycle configuration at all (the real bucket today)
    bucket_config["s3"] = s3
    bucket_config["handler"](cfn_event("Create"), None)
    assert len(s3.rules) == 1 and len(s3.notification["LambdaFunctionConfigurations"]) == 1
    bucket_config["handler"](cfn_event("Delete"), None)
    assert s3.deleted_lifecycle and s3.notification["LambdaFunctionConfigurations"] == []
    assert statuses(bucket_config) == ["SUCCESS", "SUCCESS"]


def test_custom_resource_reports_an_aws_error(bucket_config):
    class Broken(FakeBucketS3):
        def put_bucket_notification_configuration(self, Bucket, NotificationConfiguration):
            raise RuntimeError("Unable to validate the following destination configurations")
    bucket_config["s3"] = Broken()
    bucket_config["handler"](cfn_event("Create"), None)
    body = bucket_config["sent"][-1][2]
    assert body["Status"] == "FAILED" and "destination" in body["Reason"]


# A crash must never leave CloudFormation waiting: every failure below still sends FAILED.

def test_replies_when_the_event_is_malformed(bucket_config):
    event = cfn_event("Create")
    del event["ResourceProperties"]
    bucket_config["handler"](event, None)
    body = bucket_config["sent"][-1][2]
    assert body["Status"] == "FAILED" and body["Reason"].startswith("KeyError")
    assert body["PhysicalResourceId"] == "udise-bucket-config-unknown"


def test_replies_when_boto3_cannot_be_imported(bucket_config, monkeypatch):
    monkeypatch.setitem(sys.modules, "boto3", None)  # makes `import boto3` raise ImportError
    bucket_config["handler"](cfn_event("Create"), None)
    assert statuses(bucket_config) == ["FAILED"]
    assert "boto3" in bucket_config["sent"][-1][2]["Reason"]


def test_replies_on_an_unexpected_error(bucket_config):
    class Weird(FakeBucketS3):
        def get_bucket_notification_configuration(self, Bucket):
            raise TypeError("something nobody expected")
    bucket_config["s3"] = Weird()
    bucket_config["handler"](cfn_event("Delete"), None)
    assert statuses(bucket_config) == ["FAILED"]
    assert bucket_config["sent"][-1][2]["Reason"] == "TypeError: something nobody expected"


def test_a_failed_reply_does_not_crash_the_function(bucket_config):
    bucket_config["s3"] = FakeBucketS3()
    bucket_config["reply_error"] = OSError("network unreachable")
    assert bucket_config["handler"](cfn_event("Create"), None) is None  # logged; ServiceTimeout ends the wait


def test_manifest_summary_counts():
    from aws.manifest_summary import summarise
    items = [{"s3_key": "a", "dataset": "facility", "year": "2024-25", "status": "PASSED", "row_count": 5},
             {"s3_key": "b", "dataset": "facility", "year": "2025-26", "status": "PASSED", "row_count": 7},
             {"s3_key": "c", "dataset": "teacher", "year": "2025-26", "status": "QUARANTINED", "row_count": 1,
              "reason": "row_count_mismatch: 1 rows, expected 3"}]
    s = summarise(items)
    assert s["by_status"] == {"PASSED": 2, "QUARANTINED": 1}
    assert s["by_dataset"] == {"facility": 2, "teacher": 1}
    assert (s["rows"], s["rows_passed"]) == (13, 12)
    assert s["not_passed"] == [("c", "QUARANTINED", "row_count_mismatch: 1 rows, expected 3")]
