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


def lambda_handler(event, context):
    run_id = getattr(context, "aws_request_id", "local")
    checked = moved = 0
    for record in event.get("Records", []):
        bucket = record["s3"]["bucket"]["name"]
        key = urllib.parse.unquote_plus(record["s3"]["object"]["key"])
        if not key.startswith(BRONZE):
            log(key=key, result="skipped", why="not under bronze/")
            continue
        data = S3.get_object(Bucket=bucket, Key=key)["Body"].read()
        fails = validate(key, data, CONTRACT)
        checked += 1
        if not fails:
            log(key=key, result="ok", bytes=len(data), rows=CONTRACT["files"][key]["rows"])
            continue
        dest = quarantine(bucket, key, data, fails, run_id)
        moved += 1
        log(key=key, result="quarantined", rule=fails[0]["rule"], reason=fails[0]["reason"],
            quarantine_key=dest, failures=[f["rule"] for f in fails])
    return {"checked": checked, "quarantined": moved}
