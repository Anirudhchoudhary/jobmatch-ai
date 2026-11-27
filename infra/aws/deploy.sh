#!/usr/bin/env bash
# shellcheck source-path=SCRIPTDIR
# Deploy JobMatch AI to AWS (us-west-2) with the AWS CLI. Run via `make deploy`.
#
# Every step is idempotent (describe, create only if missing) and records each resource it
# creates in the state file right after creating it, so `make teardown` can always remove
# everything, even after a failed run. Needs BUDGET_EMAIL (never stored) and an existing
# jobmatch/prod secret (`make secrets-set`). Never prints secret values; xtrace is forbidden.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=config.sh
source "$HERE/config.sh"
# shellcheck source=lib.sh
source "$HERE/lib.sh"

RETRY_SLEEP="${DEPLOY_RETRY_SLEEP:-10}"
RETRY_ATTEMPTS=8

[[ -n "${BUDGET_EMAIL:-}" ]] || die "BUDGET_EMAIL must be set to the address for AWS Budgets alerts"
require_tools aws docker jq git

ACCOUNT_ID="$(awsr sts get-caller-identity --query Account --output text)"
BUCKET="$BUCKET_PREFIX-$ACCOUNT_ID"
REGISTRY="$ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com"
IMAGE_TAG="$(git -C "$REPO_ROOT" rev-parse --short HEAD)"
IMAGE_URI="$REGISTRY/$ECR_REPO:$IMAGE_TAG"
LAMBDA_ROLE_ARN="arn:aws:iam::$ACCOUNT_ID:role/$LAMBDA_ROLE"
SCHEDULER_ROLE_ARN="arn:aws:iam::$ACCOUNT_ID:role/$SCHEDULER_ROLE"
CLEANUP_ARN="arn:aws:lambda:$REGION:$ACCOUNT_ID:function:$CLEANUP_FUNCTION"
TAG_KV="Key=Project,Value=$PROJECT"

# Run a create command, retrying while a just-created IAM role is not yet assumable
# (IAM is eventually consistent). Any other error is printed and fails the deploy.
retry_while_role_propagates() {
  local attempt err
  for ((attempt = 1; attempt <= RETRY_ATTEMPTS; attempt++)); do
    if err=$("$@" 2>&1 >/dev/null); then
      return 0
    fi
    if ((attempt < RETRY_ATTEMPTS)) &&
      grep -qE 'cannot be assumed|must allow AWS EventBridge Scheduler to assume' <<<"$err"; then
      log "role not assumable yet (IAM propagation); retrying in ${RETRY_SLEEP}s"
      sleep "$RETRY_SLEEP"
      continue
    fi
    die "'$2 $3' failed: $err"
  done
}

check_prerequisites() {
  if [[ -f "$STATE_FILE" ]] && ! jq -e '.resources | arrays' "$STATE_FILE" >/dev/null 2>&1; then
    die "state file $STATE_FILE is unreadable; fix or remove it (make teardown sweeps by name)"
  fi
  resource_exists awsr secretsmanager describe-secret --secret-id "$SECRET_ID" ||
    die "secret $SECRET_ID does not exist: run make secrets-set first"
  if [[ -n "$(git -C "$REPO_ROOT" status --porcelain)" ]]; then
    warn "working tree has uncommitted changes; the image tag $IMAGE_TAG will not match them"
  fi
}

# Budgets come first so alerts exist for as long as anything else does.
ensure_budgets() {
  local i name budget notifications
  notifications=$(jq -cn --arg email "$BUDGET_EMAIL" '[{
      Notification: {NotificationType: "ACTUAL", ComparisonOperator: "GREATER_THAN",
                     Threshold: 100, ThresholdType: "PERCENTAGE"},
      Subscribers: [{SubscriptionType: "EMAIL", Address: $email}]}]')
  for i in "${!BUDGET_NAMES[@]}"; do
    name="${BUDGET_NAMES[$i]}"
    if resource_exists awsb budgets describe-budget --account-id "$ACCOUNT_ID" --budget-name "$name"; then
      continue
    fi
    log "creating budget $name (\$${BUDGET_AMOUNTS[$i]}/month, alert at 100% actual)"
    budget=$(jq -cn --arg name "$name" --arg amount "${BUDGET_AMOUNTS[$i]}" \
      '{BudgetName: $name, BudgetLimit: {Amount: $amount, Unit: "USD"},
        TimeUnit: "MONTHLY", BudgetType: "COST"}')
    awsb budgets create-budget --account-id "$ACCOUNT_ID" --budget "$budget" \
      --notifications-with-subscribers "$notifications" --resource-tags "$TAG_KV"
    state_add budget "$name"
  done
}

ensure_ecr_repository() {
  if ! resource_exists awsr ecr describe-repositories --repository-names "$ECR_REPO"; then
    log "creating ECR repository $ECR_REPO"
    awsr ecr create-repository --repository-name "$ECR_REPO" \
      --image-scanning-configuration scanOnPush=true --tags "$TAG_KV" >/dev/null
    state_add ecr_repository "$ECR_REPO"
  fi
  awsr ecr put-lifecycle-policy --repository-name "$ECR_REPO" \
    --lifecycle-policy-text "file://$POLICY_DIR/ecr-lifecycle.json" >/dev/null
}

build_and_push_image() {
  log "building and pushing $IMAGE_URI"
  # The password goes to docker on stdin only.
  awsr ecr get-login-password | docker login --username AWS --password-stdin "$REGISTRY" >/dev/null
  # BuildKit (buildx) adds an attestation manifest list that Lambda rejects, so turn it off;
  # the legacy builder has no --provenance flag and already pushes a single-image manifest.
  local build_flags=(--platform linux/amd64)
  if docker buildx version >/dev/null 2>&1; then
    build_flags+=(--provenance=false)
  fi
  docker build "${build_flags[@]}" -t "$IMAGE_URI" "$REPO_ROOT/backend"
  docker push "$IMAGE_URI"
}

ensure_bucket() {
  if ! resource_exists awsr s3api head-bucket --bucket "$BUCKET"; then
    log "creating S3 bucket $BUCKET"
    awsr s3api create-bucket --bucket "$BUCKET" \
      --create-bucket-configuration "LocationConstraint=$REGION" >/dev/null
    state_add s3_bucket "$BUCKET"
  fi
  awsr s3api put-public-access-block --bucket "$BUCKET" --public-access-block-configuration \
    BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
  awsr s3api put-bucket-encryption --bucket "$BUCKET" --server-side-encryption-configuration \
    '{"Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]}'
  awsr s3api put-bucket-lifecycle-configuration --bucket "$BUCKET" \
    --lifecycle-configuration "file://$POLICY_DIR/s3-lifecycle.json"
  awsr s3api put-bucket-tagging --bucket "$BUCKET" --tagging "TagSet=[{$TAG_KV}]"
}

ensure_log_group() {
  local name=$1 found
  found=$(awsr logs describe-log-groups --log-group-name-prefix "$name" \
    --query "length(logGroups[?logGroupName=='$name'])" --output text)
  if [[ "$found" == 0 ]]; then
    log "creating log group $name"
    awsr logs create-log-group --log-group-name "$name" --tags "Project=$PROJECT"
    state_add log_group "$name"
  fi
  awsr logs put-retention-policy --log-group-name "$name" --retention-in-days "$LOG_RETENTION_DAYS"
}

# ensure_role <role> <trust template> <inline policy name> <policy template>
ensure_role() {
  local role=$1 trust=$2 policy_name=$3 policy=$4
  if ! resource_exists awsr iam get-role --role-name "$role"; then
    log "creating IAM role $role"
    awsr iam create-role --role-name "$role" \
      --assume-role-policy-document "$(render_policy "$trust")" --tags "$TAG_KV" >/dev/null
    state_add iam_role "$role"
  fi
  awsr iam put-role-policy --role-name "$role" --policy-name "$policy_name" \
    --policy-document "$(render_policy "$policy")"
  state_add iam_role_policy "$role/$policy_name"
}

# Lambda environment: settings only, never secrets (those come from SECRET_ID at runtime).
function_environment() {
  jq -cn --arg bucket "$BUCKET" --arg secret_id "$SECRET_ID" --argjson extra "$1" '{Variables: ({
      ENV: "prod", SECRETS_SOURCE: "aws", SECRET_ID: $secret_id,
      LLM_PROVIDER: "anthropic", EMBEDDER_PROVIDER: "bedrock", RERANKER_PROVIDER: "bedrock",
      BLOB_BACKEND: "s3", UPLOADS_BUCKET: $bucket, EMAIL_SENDER: "resend",
      PII_ENGINE: "presidio"} + $extra)}'
}

# ensure_function <name> <timeout seconds> <extra env JSON object>
ensure_function() {
  local name=$1 timeout=$2 env
  env=$(function_environment "$3")
  if resource_exists awsr lambda get-function --function-name "$name"; then
    log "updating $name to image $IMAGE_TAG"
    awsr lambda update-function-code --function-name "$name" --image-uri "$IMAGE_URI" >/dev/null
    awsr lambda wait function-updated-v2 --function-name "$name"
    awsr lambda update-function-configuration --function-name "$name" --role "$LAMBDA_ROLE_ARN" \
      --memory-size "$MEMORY_MB" --timeout "$timeout" --environment "$env" >/dev/null
  else
    log "creating Lambda function $name"
    retry_while_role_propagates awsr lambda create-function --function-name "$name" \
      --package-type Image --code "ImageUri=$IMAGE_URI" --role "$LAMBDA_ROLE_ARN" \
      --architectures x86_64 --memory-size "$MEMORY_MB" --timeout "$timeout" \
      --environment "$env" --tags "Project=$PROJECT"
    state_add lambda_function "$name"
    awsr lambda wait function-active-v2 --function-name "$name"
  fi
  awsr lambda wait function-updated-v2 --function-name "$name"
}

reserve_concurrency() {
  local err
  if ! err=$(awsr lambda put-function-concurrency --function-name "$API_FUNCTION" \
    --reserved-concurrent-executions "$RESERVED_CONCURRENCY" 2>&1 >/dev/null); then
    warn "could not reserve $RESERVED_CONCURRENCY concurrent executions for $API_FUNCTION" \
      "(new accounts often have a limit of 10): $err"
    warn "continuing WITHOUT a concurrency cap: the public Function URL can scale to the" \
      "account limit, and only the AWS Budgets email alerts remain as a cost backstop"
  fi
}

# add_public_permission <statement id> <add-permission args...>
add_public_permission() {
  local sid=$1 err
  shift
  if err=$(awsr lambda add-permission --function-name "$API_FUNCTION" --statement-id "$sid" \
    --principal '*' "$@" 2>&1 >/dev/null); then
    state_add lambda_permission "$API_FUNCTION/$sid"
  elif ! grep -q ResourceConflictException <<<"$err"; then
    die "add-permission $sid failed: $err"
  fi
}

ensure_function_url() {
  if resource_exists awsr lambda get-function-url-config --function-name "$API_FUNCTION"; then
    awsr lambda update-function-url-config --function-name "$API_FUNCTION" \
      --auth-type NONE --invoke-mode RESPONSE_STREAM >/dev/null
  else
    log "creating the public Function URL for $API_FUNCTION"
    awsr lambda create-function-url-config --function-name "$API_FUNCTION" \
      --auth-type NONE --invoke-mode RESPONSE_STREAM >/dev/null
    state_add function_url "$API_FUNCTION"
  fi
  # A public (AuthType NONE) URL needs both statements.
  add_public_permission "$URL_INVOKE_URL_SID" \
    --action lambda:InvokeFunctionUrl --function-url-auth-type NONE
  add_public_permission "$URL_INVOKE_FUNCTION_SID" \
    --action lambda:InvokeFunction --invoked-via-function-url
  FUNCTION_URL=$(awsr lambda get-function-url-config --function-name "$API_FUNCTION" \
    --query FunctionUrl --output text)
  state_set_url "$FUNCTION_URL"
}

ensure_schedule() {
  local target
  target=$(jq -cn --arg arn "$CLEANUP_ARN" --arg role "$SCHEDULER_ROLE_ARN" \
    '{Arn: $arn, RoleArn: $role, Input: "{\"source\": \"jobmatch.cleanup\"}",
      RetryPolicy: {MaximumRetryAttempts: 2, MaximumEventAgeInSeconds: 3600}}')
  if resource_exists awsr scheduler get-schedule --name "$SCHEDULE_NAME"; then
    awsr scheduler update-schedule --name "$SCHEDULE_NAME" \
      --schedule-expression "$SCHEDULE_EXPRESSION" --flexible-time-window Mode=OFF \
      --target "$target" --state ENABLED >/dev/null
  else
    log "creating schedule $SCHEDULE_NAME ($SCHEDULE_EXPRESSION)"
    retry_while_role_propagates awsr scheduler create-schedule --name "$SCHEDULE_NAME" \
      --schedule-expression "$SCHEDULE_EXPRESSION" --flexible-time-window Mode=OFF \
      --target "$target" --state ENABLED
    state_add schedule "$SCHEDULE_NAME"
  fi
}

main() {
  log "deploying to account $ACCOUNT_ID, region $REGION; state file $STATE_FILE"
  check_prerequisites
  ensure_budgets
  ensure_ecr_repository
  build_and_push_image
  ensure_bucket
  ensure_log_group "/aws/lambda/$API_FUNCTION"
  ensure_log_group "/aws/lambda/$CLEANUP_FUNCTION"
  ensure_role "$LAMBDA_ROLE" lambda-trust.json "$LAMBDA_POLICY" lambda-access.json
  ensure_function "$API_FUNCTION" "$TIMEOUT_S" '{}'
  reserve_concurrency
  ensure_function_url
  # Scheduler invokes asynchronously, which a response-streaming adapter cannot answer.
  ensure_function "$CLEANUP_FUNCTION" "$CLEANUP_TIMEOUT_S" \
    '{"CLEANUP_MODE": "true", "AWS_LWA_INVOKE_MODE": "buffered"}'
  ensure_role "$SCHEDULER_ROLE" scheduler-trust.json "$SCHEDULER_POLICY" scheduler-invoke.json
  ensure_schedule
  log "deployed image $IMAGE_TAG"
  log "Function URL: $FUNCTION_URL"
  log "next: make migrate-prod (first deploy or new migrations), then make smoke-prod"
}

main "$@"
