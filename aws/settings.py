"""Names shared by the AWS scripts (one place to change them).

The region is fixed: the account is an AWS Free plan "project", and all its resources live
in one region. Credentials are never stored here: boto3 reads them from the AWS CLI profile
(`aws login --profile udise`).
"""
PROFILE = "udise"
REGION = "ap-southeast-2"
BUCKET = "udise-lake-aditya-2026"
STACK = "udise-aws"
WORKGROUP = "udise-wg"
DATABASE = "udise"
MANIFEST_TABLE = "udise-ingest-manifest"   # DynamoDB, one item per bronze file (template.yaml)

BRONZE_PREFIX = "bronze/"
QUARANTINE_FILES_PREFIX = "quarantine/files/"


def session(profile: str = PROFILE):
    """A boto3 session for the project profile, always in the project region."""
    import boto3  # imported here so modules that only need the names work without boto3
    return boto3.Session(profile_name=profile, region_name=REGION)
