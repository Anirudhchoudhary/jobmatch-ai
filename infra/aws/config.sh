# shellcheck shell=bash disable=SC2034  # used by the scripts that source this file
# Names and sizes for the JobMatch AI AWS deployment. Sourced by deploy.sh and teardown.sh;
# teardown also sweeps these names, so a lost state file still cleans everything up.
# Nothing secret belongs here.

REGION=us-west-2
BUDGETS_REGION=us-east-1 # AWS Budgets is a global service served from us-east-1
# CloudFront and Route 53 are global; a CloudFront certificate must live in ACM us-east-1.
EDGE_REGION=us-east-1
PROJECT=jobmatch-ai

API_FUNCTION=jobmatch-api
CLEANUP_FUNCTION=jobmatch-cleanup
ECR_REPO=jobmatch-api
LAMBDA_ROLE=jobmatch-lambda
LAMBDA_POLICY=jobmatch-lambda-access
SCHEDULER_ROLE=jobmatch-scheduler
SCHEDULER_POLICY=jobmatch-scheduler-invoke
SECRET_ID=jobmatch/prod
SCHEDULE_NAME=jobmatch-cleanup-hourly
SCHEDULE_EXPRESSION="rate(1 hour)"
BUCKET_PREFIX=jobmatch-uploads # full name: jobmatch-uploads-<account-id>
LOG_RETENTION_DAYS=14

# Function URL resource-policy statement ids (public access needs both since Oct 2025).
URL_INVOKE_URL_SID=FunctionURLAllowPublicAccess
URL_INVOKE_FUNCTION_SID=FunctionURLAllowInvokeViaUrl

# Monthly cost budgets: name and USD amount; each alerts BUDGET_EMAIL at 100% of actual.
BUDGET_NAMES=(jobmatch-10 jobmatch-25)
BUDGET_AMOUNTS=(10 25)

# ---- site: https://jobmatch.ntrye.com (static export in S3 + /api/* to the Function URL)
DOMAIN=jobmatch.ntrye.com
# The author's hand-made zone holds the whole domain (site, email, DKIM). It is looked up by
# name and never created, modified outside the jobmatch.ntrye.com names, or deleted.
HOSTED_ZONE=ntrye.com.
SITE_BUCKET_PREFIX=jobmatch-site # full name: jobmatch-site-<account-id>
OAC_NAME=jobmatch-site-oac
SITE_FUNCTION=jobmatch-index-rewrite # CloudFront Function: /app/ -> /app/index.html
DISTRIBUTION_COMMENT="jobmatch-ai: $DOMAIN" # how teardown finds the distribution by name
CLOUDFRONT_ZONE_ID=Z2FDTNDATAQYW2           # Route 53 alias target zone of every distribution
# Managed CloudFront policies (ids from the CloudFront Developer Guide). The origin request
# policy forwards everything except Host (the Function URL needs its own Host) plus
# CloudFront's viewer-location headers, which include CloudFront-Viewer-Address.
CACHE_POLICY_CACHING_OPTIMIZED=658327ea-f89d-4fab-a63d-7e88639e58f6
CACHE_POLICY_CACHING_DISABLED=4135ea2d-6df8-44a3-9df3-4b5a84be39ad
ORIGIN_REQUEST_POLICY_ALL_VIEWER_EXCEPT_HOST=b689b0a8-53d0-40ab-baf2-68738e2966ac
# Longest wait for the next byte from the API without a quota increase. SSE streams send
# tokens continuously; one silent for longer than this is cut by CloudFront.
ORIGIN_READ_TIMEOUT_S=60
ACM_VALIDATION_TIMEOUT_S=1200 # give up waiting for DNS validation after 20 minutes

MEMORY_MB=2048
TIMEOUT_S=300
CLEANUP_TIMEOUT_S=120
RESERVED_CONCURRENCY=5

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
POLICY_DIR="$REPO_ROOT/infra/aws/policies"
STATE_FILE="${STATE_FILE:-$REPO_ROOT/var/deploy/state.json}"
