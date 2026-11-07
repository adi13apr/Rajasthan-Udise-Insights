#!/usr/bin/env bash
# Remove the AWS layer.
#
#   aws/teardown.sh           delete the stack "udise-aws": Athena workgroup udise-wg, both Lambda
#                             functions, their IAM roles and log groups, the S3 trigger on bronze/
#                             and the athena-results/ lifecycle rule; then the packaged Lambda
#                             code under artifacts/.
#   aws/teardown.sh --data    also drop the Athena tables the pipeline created and delete its S3
#                             data (bronze/, silver/, quarantine/, gold/, reference/).
#   --yes                     do not ask for confirmation.
#
# Never deletes the bucket, manual-test/, athena-results/ or the table udise.manual_facility.
set -euo pipefail

PROFILE="${AWS_PROFILE_NAME:-udise}"
REGION="ap-southeast-2"
BUCKET="udise-lake-aditya-2026"
STACK="udise-aws"
DATABASE="udise"
AWS=(aws --profile "$PROFILE" --region "$REGION")

DATA=0; YES=0
for arg in "$@"; do
  case "$arg" in
    --data) DATA=1 ;;
    --yes) YES=1 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

# Tables written by aws/athena_build.py (never manual_facility).
GOLD_TABLES="dim_year dim_district dim_management dim_grade dim_gender dim_social_category dim_school
fact_enrolment fact_teachers fact_school_facilities int_school_year mart_district_year
mart_district_year_management"
DATA_PREFIXES="bronze silver quarantine gold reference"

echo "Account $("${AWS[@]}" sts get-caller-identity --query Account --output text), region $REGION"
echo "Will delete: stack $STACK and s3://$BUCKET/artifacts/"
if [ "$DATA" = 1 ]; then
  echo "and (--data): Athena tables bronze_*, ref_*, silver_*, quarantine_*, gold tables"
  echo "              and s3://$BUCKET/{${DATA_PREFIXES// /,}}/"
fi
if [ "$YES" != 1 ]; then
  read -r -p "Type 'delete' to continue: " answer
  [ "$answer" = "delete" ] || { echo "Cancelled."; exit 1; }
fi

if [ "$DATA" = 1 ]; then
  names=$("${AWS[@]}" glue get-tables --database-name "$DATABASE" --query 'TableList[].Name' --output text || true)
  drop=()
  for t in $names; do
    case "$t" in
      manual_facility) ;;  # the user's own table: keep
      bronze_*|ref_*|silver_*|quarantine_*) drop+=("$t") ;;
      *) for g in $GOLD_TABLES; do [ "$t" = "$g" ] && drop+=("$t"); done ;;
    esac
  done
  if [ "${#drop[@]}" -gt 0 ]; then
    echo "Dropping ${#drop[@]} tables from $DATABASE: ${drop[*]}"
    "${AWS[@]}" glue batch-delete-table --database-name "$DATABASE" --tables-to-delete "${drop[@]}" >/dev/null
  fi
  for p in $DATA_PREFIXES; do
    "${AWS[@]}" s3 rm "s3://$BUCKET/$p/" --recursive --only-show-errors
    echo "Deleted s3://$BUCKET/$p/"
  done
fi

echo "Deleting stack $STACK ..."
"${AWS[@]}" cloudformation delete-stack --stack-name "$STACK"
"${AWS[@]}" cloudformation wait stack-delete-complete --stack-name "$STACK"
"${AWS[@]}" s3 rm "s3://$BUCKET/artifacts/" --recursive --only-show-errors
echo "Stack deleted; s3://$BUCKET/artifacts/ removed."

# Check that the custom resource removed its settings from the bucket.
left=$("${AWS[@]}" s3api get-bucket-notification-configuration --bucket "$BUCKET" \
  --query "LambdaFunctionConfigurations[?Id=='udise-bronze-validator'] | length(@)" --output text)
rule=$("${AWS[@]}" s3api get-bucket-lifecycle-configuration --bucket "$BUCKET" \
  --query "Rules[?ID=='udise-athena-results-expiry'] | length(@)" --output text 2>/dev/null || echo 0)
echo "Bucket trigger entries left: ${left:-0}; lifecycle rule entries left: ${rule:-0} (both should be 0)."
