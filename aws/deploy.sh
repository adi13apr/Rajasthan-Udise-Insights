#!/usr/bin/env bash
# Deploy the AWS layer (aws/template.yaml) as the CloudFormation stack "udise-aws".
#
#   aws/deploy.sh            package the Lambda code, then create or update the stack
#
# Uses the AWS CLI profile "udise" (override with AWS_PROFILE_NAME) in ap-southeast-2.
# Credentials come from `aws login --profile udise`; nothing is stored in the repo.
set -euo pipefail

PROFILE="${AWS_PROFILE_NAME:-udise}"
REGION="ap-southeast-2"
BUCKET="udise-lake-aditya-2026"
STACK="udise-aws"
HERE="$(cd "$(dirname "$0")" && pwd)"
AWS=(aws --profile "$PROFILE" --region "$REGION")

echo "Account: $("${AWS[@]}" sts get-caller-identity --query Account --output text)  region: $REGION  stack: $STACK"

if [ ! -f "$HERE/bronze_validator/contract.json" ]; then
  echo "aws/bronze_validator/contract.json is missing: run 'python -m aws.make_contract' first." >&2
  exit 1
fi

# Optional static check of the template (pip install -r requirements-aws.txt).
if command -v cfn-lint >/dev/null 2>&1; then
  # W3002 = "Code: bronze_validator/ needs 'aws cloudformation package'", which step 1 does.
  cfn-lint --ignore-checks W3002 -- "$HERE/template.yaml"
fi

mkdir -p "$HERE/build"
# Keep test leftovers out of the Lambda zip.
find "$HERE/bronze_validator" -name __pycache__ -type d -prune -exec rm -rf {} +
# 1. Zip aws/bronze_validator/ and upload it to s3://<bucket>/artifacts/; write a template that points to it.
"${AWS[@]}" cloudformation package \
  --template-file "$HERE/template.yaml" \
  --s3-bucket "$BUCKET" --s3-prefix artifacts \
  --output-template-file "$HERE/build/packaged.yaml"

# 2. Create the stack, or update it with only what changed (a change set).
"${AWS[@]}" cloudformation deploy \
  --template-file "$HERE/build/packaged.yaml" \
  --stack-name "$STACK" \
  --capabilities CAPABILITY_NAMED_IAM \
  --tags project=udise \
  --no-fail-on-empty-changeset

"${AWS[@]}" cloudformation describe-stacks --stack-name "$STACK" \
  --query 'Stacks[0].[StackStatus, Outputs]' --output json
