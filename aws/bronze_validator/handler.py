"""Lambda entry point: validate each bronze upload; move a failing file to quarantine; record
every result in the DynamoDB ingestion manifest.

Triggered by S3 (ObjectCreated, prefix bronze/, suffix .parquet; set up by aws/template.yaml).
For each uploaded file:
    1. look it up in the manifest (table MANIFEST_TABLE, key s3_key). If the manifest says this
       exact file (same S3 etag) already PASSED, stop: result "unchanged" (an identical
       re-upload is not checked twice and nothing is written);
    2. read it from S3 and run the contract checks (validator.py, no AWS calls);
    3. passes  -> leave it in bronze/, write a PASSED item to the manifest;
       fails   -> write the file to quarantine/files/<dataset>/year=<Y>/<name>.parquet and a
                  <name>.parquet.reason.json next to it (_rule, _reason, _run_id, every failure,
                  expected vs actual), write a QUARANTINED item to the manifest, and only then
                  delete it from bronze/;
    4. log one JSON line.
The quarantine copy and the manifest item are written before the bronze file is deleted: if any
step fails, the file is still in bronze/ and S3's retry runs the whole check again.
Writing to quarantine/ cannot trigger this function again (the trigger is on bronze/ only).

Manifest item (one per file, overwritten by each new check of that key):
    s3_key, dataset, year, row_count (from the file's footer), etag, status (PASSED or
    QUARANTINED), reason (the first failing rule and why), validated_at (UTC), run_id (the
    Lambda request id), and quarantine_key for a quarantined file.
A QUARANTINED key is always checked again on the next upload, even with the same etag: the
manifest only lets a file skip the checks it has already passed.

It can also be called directly (aws lambda invoke), for files already in bronze/:
    {"keys": ["bronze/.../x.parquet", ...]}                  validate, quarantine failures
    {"keys": [...], "force": true}                           also re-check files that PASSED
    {"keys": [...], "dry_run": true}                         validate only: move nothing, write
                                                             nothing, never skip
The bucket is BUCKET_NAME (template.yaml). Used by the smoke test in aws/deploy.sh (dry run)
and by aws/revalidate_bronze.py. The reply lists each key's result.

Permissions (template.yaml): get/delete bronze/*, put quarantine/files/*, GetItem/PutItem on
the manifest table, own log group.
"""
import json
import os
import urllib.parse
from datetime import datetime, timezone

import boto3

from validator import footer_rows, key_parts, load_contract, validate

S3 = boto3.client("s3")
CONTRACT = load_contract()
BRONZE = "bronze/"
QUARANTINE = os.environ.get("QUARANTINE_PREFIX", "quarantine/files/")
BUCKET = os.environ.get("BUCKET_NAME", "")
MANIFEST = boto3.resource("dynamodb").Table(os.environ.get("MANIFEST_TABLE", "udise-ingest-manifest"))
PASSED, QUARANTINED = "PASSED", "QUARANTINED"


def log(**fields) -> None:
    print(json.dumps(fields, default=str))


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def etag_of(response: dict) -> str:
    return response["ETag"].strip('"')


def manifest_item(key: str) -> dict | None:
    # Strongly consistent: a write that finished just before is always seen.
    return MANIFEST.get_item(Key={"s3_key": key}, ConsistentRead=True).get("Item")


def record(key: str, etag: str, status: str, rows: int | None, reason: str, run_id: str, **extra) -> None:
    dataset, year = key_parts(key)
    MANIFEST.put_item(Item={"s3_key": key, "dataset": dataset, "year": year, "row_count": rows,
                            "etag": etag, "status": status, "reason": reason, "validated_at": now(),
                            "run_id": run_id, **extra})


def copy_to_quarantine(bucket: str, key: str, data: bytes, fails: list[dict], run_id: str) -> str:
    dest = QUARANTINE + key[len(BRONZE):]
    reason = {
        "source_key": key,
        "quarantine_key": dest,
        "_rule": fails[0]["rule"],
        "_reason": fails[0]["reason"],
        "_run_id": run_id,
        "checked_at": now(),
        "bytes": len(data),
        "failures": fails,
    }
    S3.put_object(Bucket=bucket, Key=dest, Body=data, ContentType="application/vnd.apache.parquet")
    S3.put_object(Bucket=bucket, Key=dest + ".reason.json", Body=json.dumps(reason, indent=2).encode(),
                  ContentType="application/json")
    return dest


def check(bucket: str, key: str, run_id: str, dry_run: bool = False, force: bool = False) -> dict:
    """Validate one bronze file; quarantine it if it fails (unless dry_run); record the result
    in the manifest (unless dry_run). Returns its result."""
    if not key.startswith(BRONZE):
        result = {"key": key, "result": "skipped", "why": "not under bronze/"}
        log(**result)
        return result

    previous = manifest_item(key)
    if previous and not (dry_run or force) and previous.get("status") == PASSED:
        etag = etag_of(S3.head_object(Bucket=bucket, Key=key))
        if previous.get("etag") == etag:
            result = {"key": key, "result": "unchanged", "etag": etag,
                      "why": f"same etag already PASSED at {previous.get('validated_at')}"}
            log(**result, dry_run=dry_run)
            return result

    obj = S3.get_object(Bucket=bucket, Key=key)
    etag, data = etag_of(obj), obj["Body"].read()   # the etag of the bytes actually checked
    fails = validate(key, data, CONTRACT)
    if not fails:
        rows = CONTRACT["files"][key]["rows"]
        if not dry_run:
            record(key, etag, PASSED, rows, "all contract checks passed", run_id)
        result = {"key": key, "result": "ok", "bytes": len(data), "rows": rows, "etag": etag}
    elif dry_run:
        result = {"key": key, "result": "would_quarantine", "rule": fails[0]["rule"], "reason": fails[0]["reason"]}
    else:
        dest = copy_to_quarantine(bucket, key, data, fails, run_id)
        record(key, etag, QUARANTINED, footer_rows(data), f"{fails[0]['rule']}: {fails[0]['reason']}", run_id,
               quarantine_key=dest)
        S3.delete_object(Bucket=bucket, Key=key)   # last: copy and manifest item are already written
        result = {"key": key, "result": "quarantined", "rule": fails[0]["rule"], "reason": fails[0]["reason"],
                  "quarantine_key": dest, "failures": [f["rule"] for f in fails], "etag": etag}
    if previous:
        result["previous_status"] = previous.get("status")
    log(**result, dry_run=dry_run)
    return result


def lambda_handler(event, context):
    run_id = getattr(context, "aws_request_id", "local")
    if "Records" in event:   # S3 upload notifications
        targets = [(r["s3"]["bucket"]["name"], urllib.parse.unquote_plus(r["s3"]["object"]["key"]))
                   for r in event["Records"]]
    else:                    # direct call: {"keys": [...], "dry_run": true|false, "force": true|false}
        targets = [(event.get("bucket") or BUCKET, k) for k in event.get("keys", [])]
    dry_run = bool(event.get("dry_run", False))
    force = bool(event.get("force", False))
    results = [check(bucket, key, run_id, dry_run, force) for bucket, key in targets]
    return {"checked": sum(r["result"] not in ("skipped", "unchanged") for r in results),
            "unchanged": sum(r["result"] == "unchanged" for r in results),
            "quarantined": sum(r["result"] == "quarantined" for r in results),
            "would_quarantine": sum(r["result"] == "would_quarantine" for r in results),
            "results": results}
