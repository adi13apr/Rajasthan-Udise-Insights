"""Lambda entry point: validate each bronze upload; move a failing file to quarantine.

Triggered by S3 (ObjectCreated, prefix bronze/, suffix .parquet; set up by aws/template.yaml).
For each uploaded file:
    1. read it from S3;
    2. run the contract checks (validator.py, no AWS calls);
    3. passes  -> leave it in bronze/ and log one JSON line;
       fails   -> write the file to quarantine/files/<dataset>/year=<Y>/<name>.parquet and a
                  <name>.parquet.reason.json next to it (_rule, _reason, _run_id, every failure,
                  expected vs actual), and only then delete it from bronze/.
The quarantine copy is written before the bronze file is deleted, so a file is never lost.
Writing to quarantine/ cannot trigger this function again (the trigger is on bronze/ only).

It can also be called directly (aws lambda invoke), for files already in bronze/:
    {"keys": ["bronze/.../x.parquet", ...]}                  validate, quarantine failures
    {"keys": [...], "dry_run": true}                         validate only, move nothing
The bucket is BUCKET_NAME (template.yaml). Used by the smoke test in aws/deploy.sh (dry run)
and by aws/revalidate_bronze.py. The reply lists each key's result.

Permissions (template.yaml): get/delete bronze/*, put quarantine/files/*, own log group.
"""
import json
import os
import urllib.parse
from datetime import datetime, timezone

import boto3

from validator import load_contract, validate

S3 = boto3.client("s3")
CONTRACT = load_contract()
BRONZE = "bronze/"
QUARANTINE = os.environ.get("QUARANTINE_PREFIX", "quarantine/files/")
BUCKET = os.environ.get("BUCKET_NAME", "")


def log(**fields) -> None:
    print(json.dumps(fields, default=str))


def quarantine(bucket: str, key: str, data: bytes, fails: list[dict], run_id: str) -> str:
    dest = QUARANTINE + key[len(BRONZE):]
    reason = {
        "source_key": key,
        "quarantine_key": dest,
        "_rule": fails[0]["rule"],
        "_reason": fails[0]["reason"],
        "_run_id": run_id,
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "bytes": len(data),
        "failures": fails,
    }
    S3.put_object(Bucket=bucket, Key=dest, Body=data, ContentType="application/vnd.apache.parquet")
    S3.put_object(Bucket=bucket, Key=dest + ".reason.json", Body=json.dumps(reason, indent=2).encode(),
                  ContentType="application/json")
    S3.delete_object(Bucket=bucket, Key=key)
    return dest


def check(bucket: str, key: str, run_id: str, dry_run: bool = False) -> dict:
    """Validate one bronze file; quarantine it if it fails (unless dry_run). Returns its result."""
    if not key.startswith(BRONZE):
        result = {"key": key, "result": "skipped", "why": "not under bronze/"}
        log(**result)
        return result
    data = S3.get_object(Bucket=bucket, Key=key)["Body"].read()
    fails = validate(key, data, CONTRACT)
    if not fails:
        result = {"key": key, "result": "ok", "bytes": len(data), "rows": CONTRACT["files"][key]["rows"]}
    elif dry_run:
        result = {"key": key, "result": "would_quarantine", "rule": fails[0]["rule"], "reason": fails[0]["reason"]}
    else:
        dest = quarantine(bucket, key, data, fails, run_id)
        result = {"key": key, "result": "quarantined", "rule": fails[0]["rule"], "reason": fails[0]["reason"],
                  "quarantine_key": dest, "failures": [f["rule"] for f in fails]}
    log(**result, dry_run=dry_run)
    return result


def lambda_handler(event, context):
    run_id = getattr(context, "aws_request_id", "local")
    if "Records" in event:   # S3 upload notifications
        targets = [(r["s3"]["bucket"]["name"], urllib.parse.unquote_plus(r["s3"]["object"]["key"]))
                   for r in event["Records"]]
    else:                    # direct call: {"keys": [...], "dry_run": true|false}
        targets = [(event.get("bucket") or BUCKET, k) for k in event.get("keys", [])]
    dry_run = bool(event.get("dry_run", False))
    results = [check(bucket, key, run_id, dry_run) for bucket, key in targets]
    return {"checked": sum(r["result"] != "skipped" for r in results),
            "quarantined": sum(r["result"] == "quarantined" for r in results),
            "would_quarantine": sum(r["result"] == "would_quarantine" for r in results),
            "results": results}
