"""Summary of the DynamoDB ingestion manifest (udise-ingest-manifest), read-only.

    python -m aws.manifest_summary        count of items by status, by dataset, by dataset x year

Scans the whole table (one item per bronze file, so under a hundred items: a scan reads a few
KB). Prints counts only, never file contents. Exit code 1 if any item is not PASSED.
"""
import argparse
import sys
from collections import Counter

from . import settings as S


def scan_items(table) -> list[dict]:
    """Every item of the table, following DynamoDB's 1 MB pages (paginator of the resource's
    own client, so values come back as plain Python types)."""
    paginator = table.meta.client.get_paginator("scan")
    return [item for page in paginator.paginate(TableName=table.name) for item in page["Items"]]


def summarise(items: list[dict]) -> dict:
    return {
        "items": len(items),
        "by_status": dict(sorted(Counter(i.get("status") for i in items).items())),
        "by_dataset": dict(sorted(Counter(i.get("dataset") for i in items).items(), key=str)),
        "by_dataset_year": dict(sorted(Counter((i.get("dataset"), i.get("year")) for i in items).items(), key=str)),
        "rows": sum(int(i.get("row_count") or 0) for i in items),
        "rows_passed": sum(int(i.get("row_count") or 0) for i in items if i.get("status") == "PASSED"),
        "not_passed": sorted((i["s3_key"], i.get("status"), i.get("reason")) for i in items
                             if i.get("status") != "PASSED"),
        "validated_at": (min((i.get("validated_at") or "" for i in items), default=None),
                         max((i.get("validated_at") or "" for i in items), default=None)),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--profile", default=S.PROFILE)
    ap.add_argument("--table", default=S.MANIFEST_TABLE)
    args = ap.parse_args(argv)

    table = S.session(args.profile).resource("dynamodb").Table(args.table)
    s = summarise(scan_items(table))
    print(f"Table {args.table}: {s['items']} items; {s['rows']:,} rows in total, {s['rows_passed']:,} in PASSED files")
    print(f"validated_at from {s['validated_at'][0]} to {s['validated_at'][1]}")
    print("\nBy status:")
    for k, v in s["by_status"].items():
        print(f"  {k:<12} {v:>4}")
    print("\nBy dataset:")
    for k, v in s["by_dataset"].items():
        print(f"  {str(k):<12} {v:>4}")
    print("\nBy dataset and year:")
    for (d, y), v in s["by_dataset_year"].items():
        print(f"  {str(d):<12} {str(y):<8} {v:>4}")
    if s["not_passed"]:
        print("\nNot PASSED:")
        for key, status, reason in s["not_passed"]:
            print(f"  {key}: {status} - {reason}")
    return 1 if s["not_passed"] else 0


if __name__ == "__main__":
    sys.exit(main())
