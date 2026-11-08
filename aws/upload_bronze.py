"""Upload the local bronze Parquet files to S3, keeping the local layout.

    data/bronze/<dataset>/year=<YYYY-YY>/<file>.parquet
        -> s3://udise-lake-aditya-2026/bronze/<dataset>/year=<YYYY-YY>/<file>.parquet

    python -m aws.upload_bronze             upload new or changed files
    python -m aws.upload_bronze --dry-run   only list what would be uploaded
    python -m aws.upload_bronze --check     after uploading, wait and report what the
                                            validator Lambda quarantined

Each upload to bronze/ triggers the validator Lambda (aws/bronze_validator/), which moves a
file that breaks the contract to quarantine/files/ with a .reason.json next to it.

Idempotent: a file whose S3 copy has the same MD5 is skipped, so re-running does not upload
(and validate) unchanged files again. Files are uploaded in one part (they are a few MB), so
the S3 ETag is the file's MD5. data/bronze/_state.json is local bookkeeping and not uploaded.
"""
import argparse
import hashlib
import sys
import time
from pathlib import Path

from pipeline import config as C

from . import settings as S

ONE_PART = 256 * 1024 * 1024  # never use multipart: keeps ETag == MD5


def md5(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def local_files(bronze_dir: Path, only: str | None = None) -> list[Path]:
    files = sorted(bronze_dir.glob("*/year=*/*.parquet"))
    return [f for f in files if only is None or f.relative_to(bronze_dir).parts[0] == only]


def s3_key(path: Path, bronze_dir: Path) -> str:
    return S.BRONZE_PREFIX + path.relative_to(bronze_dir).as_posix()


def remote_etags(s3, bucket: str, prefix: str) -> dict[str, str]:
    """key -> ETag (without quotes) for every object under the prefix."""
    out = {}
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            out[obj["Key"]] = obj["ETag"].strip('"')
    return out


def plan_uploads(files: list[Path], bronze_dir: Path, remote: dict[str, str]) -> list[tuple[Path, str, str]]:
    """(local path, S3 key, reason) for every file that is missing in S3 or differs."""
    todo = []
    for f in files:
        key = s3_key(f, bronze_dir)
        if key not in remote:
            todo.append((f, key, "new"))
        elif remote[key] != md5(f):
            todo.append((f, key, "changed"))
    return todo


def quarantine_report(s3, bucket: str, keys: list[str]) -> list[dict]:
    """Reason files the validator wrote for the given bronze keys."""
    import json
    found = []
    for key in keys:
        reason_key = S.QUARANTINE_FILES_PREFIX + key[len(S.BRONZE_PREFIX):] + ".reason.json"
        try:
            body = s3.get_object(Bucket=bucket, Key=reason_key)["Body"].read()
        except s3.exceptions.NoSuchKey:
            continue
        found.append(json.loads(body))
    return found


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--profile", default=S.PROFILE)
    ap.add_argument("--bucket", default=S.BUCKET)
    ap.add_argument("--bronze-dir", type=Path, default=C.BRONZE_DIR)
    ap.add_argument("--only", help="upload one dataset only (e.g. facility)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--check", action="store_true",
                    help="after uploading, wait for the validator and report quarantined files")
    ap.add_argument("--check-wait", type=int, default=60, help="seconds to wait before --check")
    args = ap.parse_args(argv)

    files = local_files(args.bronze_dir, args.only)
    if not files:
        print(f"No bronze Parquet files under {args.bronze_dir}; run python run_pipeline.py first.")
        return 1
    from boto3.s3.transfer import TransferConfig
    s3 = S.session(args.profile).client("s3")
    remote = remote_etags(s3, args.bucket, S.BRONZE_PREFIX)
    todo = plan_uploads(files, args.bronze_dir, remote)
    total = sum(f.stat().st_size for f, _, _ in todo)
    print(f"{len(files)} local bronze files; {len(todo)} to upload ({total / 1e6:.1f} MB), "
          f"{len(files) - len(todo)} already in s3://{args.bucket}/{S.BRONZE_PREFIX} with the same MD5.")

    if args.dry_run:
        for f, key, why in todo:
            print(f"  would upload ({why}) {key}")
        return 0

    config = TransferConfig(multipart_threshold=ONE_PART)
    t0 = time.perf_counter()
    for i, (f, key, why) in enumerate(todo, 1):
        s3.upload_file(str(f), args.bucket, key, Config=config,
                       ExtraArgs={"ContentType": "application/vnd.apache.parquet"})
        print(f"  [{i}/{len(todo)}] {why:7} {key}")
    print(f"Uploaded {len(todo)} files ({total / 1e6:.1f} MB) in {time.perf_counter() - t0:.1f} s.")

    if args.check and todo:
        print(f"Waiting {args.check_wait} s for the validator ...")
        time.sleep(args.check_wait)
        keys = [key for _, key, _ in todo]
        bad = quarantine_report(s3, args.bucket, keys)
        still = remote_etags(s3, args.bucket, S.BRONZE_PREFIX)
        print(f"Still in bronze/: {sum(k in still for k in keys)} of {len(keys)} uploaded; "
              f"quarantined: {len(bad)}.")
        for r in bad:
            print(f"  {r['source_key']}: {r['_rule']} - {r['_reason']}")
        return 1 if bad else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
