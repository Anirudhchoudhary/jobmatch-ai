# shellcheck shell=bash disable=SC2034  # used by the scripts that source this file
# Names and sizes for the JobMatch AI AWS deployment. Sourced by deploy.sh and teardown.sh;
# teardown also sweeps these names, so a lost state file still cleans everything up.
# Nothing secret belongs here.

REGION=us-west-2
BUDGETS_REGION=us-east-1 # AWS Budgets is a global service served from us-east-1
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

MEMORY_MB=2048
TIMEOUT_S=300
CLEANUP_TIMEOUT_S=120
RESERVED_CONCURRENCY=5

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
POLICY_DIR="$REPO_ROOT/infra/aws/policies"
STATE_FILE="${STATE_FILE:-$REPO_ROOT/var/deploy/state.json}"
