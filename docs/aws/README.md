# AWS screenshots

The six images here are placeholders. Replace each with a real screenshot **of the same file
name**; the main README links to them. Before you take each screenshot, make sure it shows no
school codes (pseudocode) and no school-level rows: district-level results only (UDISE+ terms).
Account IDs and ARNs are fine to show, but you may blur them.

| File | Where | What it should show |
|---|---|---|
| `s3_layers.png` | S3 console > bucket `udise-lake-aditya-2026` | The top-level prefixes: `bronze/`, `silver/`, `quarantine/`, `gold/`, `reference/`, `athena-results/` |
| `cloudformation_stack.png` | CloudFormation > Stacks > `udise-aws` | Status `UPDATE_COMPLETE` and the Resources tab (workgroup, 2 functions, 2 roles, 2 log groups, permission, `BucketConfig`) |
| `lambda_logs.png` | CloudWatch > Log groups > `/aws/lambda/udise-bronze-validator` | Log lines with `"result": "ok"` from the re-validation (94 files) |
| `athena_gold_query.png` | Athena > Query editor, workgroup `udise-wg`, database `udise` | e.g. `SELECT district, year, schools, enrolment, ptr FROM mart_district_year ORDER BY ptr DESC LIMIT 10` with its result and "Data scanned" |
| `parity_pass.png` | Terminal | `python -m aws.parity` printing `PASS: 22,194 cells compared, 0 mismatches; official 8/8 exact on Athena` |
| `billing.png` | Billing and Cost Management > Bills (or Cost Explorer, service view) | This month's charges by service (Athena, S3, Lambda) and the credits applied |

Billing data usually appears about a day after the usage. Also update the "Actual cost" line in the
main README with the figure shown.
