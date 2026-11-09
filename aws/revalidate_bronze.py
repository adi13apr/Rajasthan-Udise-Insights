"""Validate the files already in s3://<bucket>/bronze/ again, by calling the deployed validator.

    python -m aws.revalidate_bronze             validate every bronze file; failures are quarantined
    python -m aws.revalidate_bronze --force     also re-check files the manifest says already PASSED
    python -m aws.revalidate_bronze --dry-run   only report what would be quarantined

Use after the validator changed (or failed) while files were already uploaded: the S3 trigger
only fires on upload, and upload_bronze.py skips unchanged files. Calls the Lambda directly,
once per file and synchronously ({"keys": [key], "dry_run": ...}), so every file gets the same
checks as an upload, and each result is written to the DynamoDB manifest (udise-ingest-manifest)
exactly as for an upload: run once on an empty manifest, this backfills it. A file whose etag
already PASSED is reported "unchanged" and not checked again; use --force after the validator
or the contract changed. Exit code 1 if any call errors or any file is (or would be) quarantined.
"""
import argparse
import json
import sys
import time

from . import settings as S

FUNCTION = "udise-bronze-validator"


def bronze_keys(s3, bucket: str) -> list[str]:
    keys = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=S.BRONZE_PREFIX):
        keys += [o["Key"] for o in page.get("Contents", []) if o["Key"].endswith(".parquet")]
    return sorted(keys)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--profile", default=S.PROFILE)
    ap.add_argument("--bucket", default=S.BUCKET)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="re-check files that already PASSED")
    args = ap.parse_args(argv)

    session = S.session(args.profile)
    s3, lam = session.client("s3"), session.client("lambda")
    keys = bronze_keys(s3, args.bucket)
    print(f"{len(keys)} bronze files in s3://{args.bucket}/{S.BRONZE_PREFIX}; calling {FUNCTION} "
          f"({'dry run' if args.dry_run else 'failures are quarantined'}{', force' if args.force else ''}) ...")
    counts: dict[str, int] = {}
    problems = []
    t0 = time.perf_counter()
    for key in keys:
        resp = lam.invoke(FunctionName=FUNCTION, InvocationType="RequestResponse",
                          Payload=json.dumps({"keys": [key], "dry_run": args.dry_run,
                                              "force": args.force}).encode())
        body = json.loads(resp["Payload"].read())
        if resp.get("FunctionError"):
            counts["error"] = counts.get("error", 0) + 1
            problems.append(f"  {key}: function error: {body.get('errorType')}: {body.get('errorMessage')}")
            continue
        result = body["results"][0]
        counts[result["result"]] = counts.get(result["result"], 0) + 1
        if result["result"] not in ("ok", "unchanged"):
            problems.append(f"  {key}: {result['result']} - {result.get('rule', '')}: {result.get('reason', '')}")
    print(f"Done in {time.perf_counter() - t0:.1f} s: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    print("\n".join(problems) if problems else "Every file passed the contract checks.")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
