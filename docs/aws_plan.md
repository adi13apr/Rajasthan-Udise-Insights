# AWS lakehouse plan (approved 2026-11-08)

The AWS layer runs the same pipeline as the laptop, in the cloud, and proves it gives
the same answers:

```
local data/bronze  --upload-->  S3 bronze/  --S3 trigger-->  Lambda (file contract check)
                                    |                              | bad file
                                    |                              v
                                    |                      S3 quarantine/files/ + reason JSON
                                    v
                Athena: bronze tables -> silver (+ quarantine/rows) -> gold star schema (CTAS)
                                    |
                                    v
                parity: Athena gold vs DuckDB gold + official UDISE+ figures (fails on any mismatch)
```

Account: AWS Free plan (with credits), profile `udise`, region `ap-southeast-2` only.
Bucket `udise-lake-aditya-2026` already exists and is never recreated or deleted.

## Decisions

1. **Silver is built in Athena** (not uploaded from the laptop). The cloud then runs the
   whole ELT chain from bronze, and the parity check proves the whole chain, including the
   quarantine counts. The rule catalogue stays in one place: names, order and severity come
   from `pipeline/rules.py`, and only the checks are written again as SQL (as `spark/` does).
2. **The Lambda checks whole files; the row rules stay in silver.** The quarantine rules in
   `pipeline/rules.py` are row-level, and one (duplicates) compares rows across files, which a
   function looking at one file cannot judge. So the Lambda is a gate at the door: the file is
   in the contract, is readable Parquet, has exactly the expected columns and types and row
   count, and its `_year` matches its folder. It reads only the Parquet footer (columns, row
   count, min/max statistics): the AWS SDK for pandas layer's pyarrow cannot decompress zstd
   data pages (the first deploy failed on every file). `_source_row` runs 1..n needs the data,
   so it is an Athena data test on each bronze table instead
   (`sql/athena/tests/00_bronze_source_rows.sql`). A failing file moves to
   `quarantine/files/` with a `.reason.json` (`_rule`, `_reason`, expected vs actual). The
   contract (`aws/bronze_validator/contract.json`) is generated from the local bronze files; it holds
   file names, columns and row counts, no school data. A file not in the contract is
   quarantined (no expected row count); regenerate the contract when new data arrives.
   Row-level rules run in Athena silver, as they do locally; quarantined rows go to
   `quarantine/rows/<table>/`.
3. **Settings on the existing bucket go through a CloudFormation custom resource.**
   CloudFormation cannot change a bucket it did not create, so a small helper Lambda in the
   template adds the S3 trigger (prefix `bronze/`, suffix `.parquet`) and the lifecycle rule
   (delete `athena-results/` after 7 days) on create, and removes them on delete. This is
   what AWS CDK does under the hood.
4. **Deploy order:** phases 2-4 are committed separately, but the stack is deployed only once
   the real Lambda code and its tests are ready, then bronze is uploaded (so every upload is
   validated).

## What gets created in AWS (each command shown and approved first)

- Stack `udise-aws`: Athena workgroup `udise-wg` (engine v3, 1 GB scan limit per query,
  results to `athena-results/`, enforced), Lambda `udise-bronze-validator` (Python 3.13,
  AWS SDK for pandas layer for pyarrow) and its log group (7 days), its IAM role (read and
  delete only `bronze/*`, write only `quarantine/files/*`, its own logs), the bucket-settings
  helper Lambda, its role (notification and lifecycle settings of this bucket only) and log group.
- S3 prefixes written by the pipeline: `bronze/`, `silver/`, `quarantine/`, `gold/`,
  `reference/`, `artifacts/` (packaged Lambda code).
- Glue Data Catalog tables in database `udise`: `bronze_*`, `ref_*`, `silver_*`,
  `quarantine_*` and the gold tables. `udise.manual_facility` is left alone.

`aws/teardown.sh` deletes the stack (and with it the trigger and lifecycle rule).
`--data` also drops the Athena tables and deletes the pipeline's S3 data. It never deletes
the bucket, `manual-test/` or `udise.manual_facility`.

## Phases (one commit each)

1. This plan.
2. `aws/template.yaml`, `aws/deploy.sh`, `aws/teardown.sh`.
3. `aws/upload_bronze.py` (boto3): local bronze -> `s3://udise-lake-aditya-2026/bronze/<dataset>/year=<YYYY-YY>/`.
4. `aws/bronze_validator/` (validator, handler, contract; not `aws/lambda/`, a reserved word in Python) and `aws/make_contract.py`; then deploy and upload.
5. `aws/athena_build.py`: bronze tables (partitioned by year) -> silver and quarantine
   (SQL generated from the rules) -> gold with CTAS (Parquet under `gold/`, facts
   partitioned by year) -> the `sql/tests` checks, translated to Athena SQL.
6. `aws/parity.py`: official figures on Athena gold; row counts and key totals per district
   and year against DuckDB gold (whole numbers exact, ratios within 0.001). Exit 1 on any mismatch.
7. pytest tests for the Lambda checks on local fixtures, no AWS calls (CI unchanged).
8. README: AWS architecture (Mermaid), deploy / run / teardown steps, screenshots, actual cost.

AWS dependencies: `requirements-aws.txt`. No credentials are stored anywhere; scripts take
`--profile udise`.

## Not allowed (Free plan budget)

No EMR, Redshift, NAT gateways, EC2, RDS, Glue crawlers, Glue ETL jobs or QuickSight. If a
call fails because the Free plan does not allow a service, stop and report; no workarounds.

## Known risks

- Athena cannot swap tables atomically: a rebuild drops and recreates them. If the data
  tests fail the run stops and says so (unlike DuckDB, the previous copy is not kept).
- DuckDB-only SQL needs rewriting for Athena (Trino): `UNPIVOT`, `* exclude`, `arg_max`,
  `range()`, list functions, `anti join`.
- The `primary` workgroup has no result location of its own (it was set in the console's
  editor settings), so queries from code must use `udise-wg`.

## Addition: DynamoDB ingestion manifest (branch feature/aws-dynamodb-manifest)

- Table `udise-ingest-manifest` in the stack: on-demand billing, partition key `s3_key` (string),
  deleted with the stack. The validator role gets `dynamodb:GetItem` and `dynamodb:PutItem` on
  that table only.
- The validator writes one item per checked file: `s3_key, dataset, year, row_count, etag,
  status (PASSED / QUARANTINED), reason, validated_at, run_id` (+ `quarantine_key`).
- Before checking, it looks the key up: same etag already PASSED -> `unchanged`, no download.
  QUARANTINED is always re-checked. Dry runs never skip and never write; `force` re-checks.
- Backfill with `python -m aws.revalidate_bronze`; summary with `python -m aws.manifest_summary`.
