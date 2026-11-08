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
  # Relative path, run from aws/: on Windows (Git Bash) cfn-lint finds no file at /e/... paths
  # and exits 1 without a message.
  (cd "$HERE" && cfn-lint --ignore-checks W3002 -- template.yaml)
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

# 3. Smoke test: call the deployed validator on one real bronze file, in dry-run mode (it only
#    checks; nothing is moved). Fails the deploy if the function errors or the file does not
#    pass. Unit tests run with the local pyarrow; this proves the code works in the real Lambda
#    runtime and layer (the first deploy failed there on every file: no zstd in the layer).
KEY=$("${AWS[@]}" s3api list-objects-v2 --bucket "$BUCKET" --prefix bronze/ \
  --query "Contents[?ends_with(Key, '.parquet')] | [0].Key" --output text)
if [ -z "$KEY" ] || [ "$KEY" = "None" ]; then
  echo "Smoke test skipped: no bronze file in s3://$BUCKET/bronze/ yet."
else
  PAYLOAD=$(python -c 'import json, sys; print(json.dumps({"keys": [sys.argv[1]], "dry_run": True}))' "$KEY")
  (cd "$HERE" && "${AWS[@]}" lambda invoke --function-name udise-bronze-validator \
     --cli-binary-format raw-in-base64-out --payload "$PAYLOAD" build/smoke.json \
     --query FunctionError --output text > build/smoke_error.txt)
  ERR=$(cat "$HERE/build/smoke_error.txt")
  RESULT=$(python -c 'import json, sys; print(json.load(open(sys.argv[1]))["results"][0]["result"])' \
             "$HERE/build/smoke.json" 2>/dev/null || echo "unreadable reply")
  if [ "$ERR" != "None" ] || [ "$RESULT" != "ok" ]; then
    echo "SMOKE TEST FAILED on $KEY: FunctionError=$ERR result=$RESULT" >&2
    cat "$HERE/build/smoke.json" >&2
    exit 1
  fi
  echo "Smoke test passed: the deployed validator checked $KEY (dry run): ok."
fi
