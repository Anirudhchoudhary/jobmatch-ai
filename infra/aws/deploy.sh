#!/usr/bin/env bash
# shellcheck source-path=SCRIPTDIR
# Deploy JobMatch AI to AWS (us-west-2) with the AWS CLI. Run via `make deploy`.
#
# Every step is idempotent (describe, create only if missing) and records each resource it
# creates in the state file right after creating it, so `make teardown` can always remove
# everything, even after a failed run. Needs BUDGET_EMAIL (never stored), an existing
# jobmatch/prod secret (`make secrets-set`) and the hand-made public Route 53 zone ntrye.com.
# (never created, deleted or changed here except the jobmatch.ntrye.com records).
# Never prints secret values; xtrace is forbidden. Files holding secret material (the secret
# JSON, CloudFront distribution configs with the origin token) live only in a private temp
# directory that is removed on exit.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=config.sh
source "$HERE/config.sh"
# shellcheck source=lib.sh
source "$HERE/lib.sh"

RETRY_SLEEP="${DEPLOY_RETRY_SLEEP:-10}"
RETRY_ATTEMPTS=8
ACM_TIMEOUT_S="${DEPLOY_ACM_TIMEOUT_S:-$ACM_VALIDATION_TIMEOUT_S}"
ACM_POLL_SLEEP="${DEPLOY_ACM_POLL_SLEEP:-30}"

[[ -n "${BUDGET_EMAIL:-}" ]] || die "BUDGET_EMAIL must be set to the address for AWS Budgets alerts"
require_tools aws docker jq git python3

umask 077
WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT
SECRET_FILE="$WORK_DIR/secret.json"
SECRET_VERSION=""
ZONE_ID=""
CERT_ARN=""
OAC_ID=""
SITE_FUNCTION_ARN=""
DISTRIBUTION_ID=""
DISTRIBUTION_ARN=""
DISTRIBUTION_DOMAIN=""

ACCOUNT_ID="$(awsr sts get-caller-identity --query Account --output text)"
BUCKET="$BUCKET_PREFIX-$ACCOUNT_ID"
SITE_BUCKET="$SITE_BUCKET_PREFIX-$ACCOUNT_ID"
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
  ZONE_ID=$(hosted_zone_id) || die "could not look up the Route 53 hosted zone $HOSTED_ZONE"
  [[ -n "$ZONE_ID" ]] ||
    die "no public Route 53 hosted zone named $HOSTED_ZONE (it is made by hand, never by deploy)"
  # Fail before creating anything if the site's name already holds records deploy did not make.
  local type current
  for type in A AAAA; do
    current=$(record_get "$ZONE_ID" "$DOMAIN." "$type") || die "could not read the $DOMAIN records"
    foreign_record_guard "$type" "$current"
  done
  # A CNAME cannot share its name with the alias records, and deploy never makes one there.
  current=$(record_get "$ZONE_ID" "$DOMAIN." CNAME) || die "could not read the $DOMAIN records"
  [[ -z "$current" ]] ||
    die "refusing to deploy: $DOMAIN has a CNAME record, which deploy did not create and which" \
      "the site's A/AAAA alias records cannot coexist with. Remove it by hand first."
  if [[ -n "$(git -C "$REPO_ROOT" status --porcelain)" ]]; then
    warn "working tree has uncommitted changes; the image tag $IMAGE_TAG will not match them"
  fi
}

# foreign_record_guard <type> <current record set JSON or empty>: die unless the record is
# absent or a CloudFront alias (which only deploy makes at this name).
foreign_record_guard() {
  local target
  [[ -z "$2" ]] && return 0
  target=$(jq -r '.AliasTarget.HostedZoneId // empty' <<<"$2")
  [[ "$target" == "$CLOUDFRONT_ZONE_ID" ]] ||
    die "refusing to replace the $DOMAIN $1 record: it exists and is not a CloudFront alias," \
      "so deploy did not create it. Remove it by hand if it should point at the site."
}

# The API only accepts /api/* requests carrying this token, which CloudFront adds as the
# X-Origin-Verify origin header. Merged into the existing secret JSON; never printed.
ensure_origin_verify_token() {
  local merged="$WORK_DIR/secret-merged.json" status
  awsr secretsmanager get-secret-value --secret-id "$SECRET_ID" --query SecretString \
    --output text >"$SECRET_FILE"
  status=$(python3 -I "$HERE/origin_token.py" "$SECRET_FILE" "$merged") ||
    die "could not read secret $SECRET_ID as a JSON object"
  if [[ "$status" == added ]]; then
    awsr secretsmanager put-secret-value --secret-id "$SECRET_ID" \
      --secret-string "file://$merged" >/dev/null
    mv "$merged" "$SECRET_FILE"
    log "generated ORIGIN_VERIFY_TOKEN and added it to secret $SECRET_ID (value not shown)"
  else
    log "ORIGIN_VERIFY_TOKEN is already set in secret $SECRET_ID"
  fi
}

# The secret's current VersionId (an id, not secret material). Functions read the secret at
# cold start, so it goes into their environment as CONFIG_REV: any change to the secret (a new
# token, or `make secrets-set` rotating a key) changes the configuration on the next deploy,
# which replaces the warm instances; an unchanged secret leaves them running.
read_secret_version() {
  SECRET_VERSION=$(awsr secretsmanager describe-secret --secret-id "$SECRET_ID" \
    --query VersionIdsToStages --output json |
    jq -r 'to_entries[] | select(.value | index("AWSCURRENT")) | .key') ||
    die "could not read the current version of secret $SECRET_ID"
  [[ -n "$SECRET_VERSION" ]] || die "secret $SECRET_ID has no AWSCURRENT version"
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

# ensure_private_bucket <name>: block all public access, SSE-S3, tagged.
ensure_private_bucket() {
  local bucket=$1
  if ! resource_exists awsr s3api head-bucket --bucket "$bucket"; then
    log "creating S3 bucket $bucket"
    awsr s3api create-bucket --bucket "$bucket" \
      --create-bucket-configuration "LocationConstraint=$REGION" >/dev/null
    state_add s3_bucket "$bucket"
  fi
  awsr s3api put-public-access-block --bucket "$bucket" --public-access-block-configuration \
    BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
  awsr s3api put-bucket-encryption --bucket "$bucket" --server-side-encryption-configuration \
    '{"Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]}'
  awsr s3api put-bucket-tagging --bucket "$bucket" --tagging "TagSet=[{$TAG_KV}]"
}

# Uploads: expire stale objects; browsers PUT files to presigned URLs from the site only.
ensure_bucket() {
  ensure_private_bucket "$BUCKET"
  awsr s3api put-bucket-lifecycle-configuration --bucket "$BUCKET" \
    --lifecycle-configuration "file://$POLICY_DIR/s3-lifecycle.json"
  awsr s3api put-bucket-cors --bucket "$BUCKET" \
    --cors-configuration "$(render_policy s3-cors.json)"
  state_add s3_bucket_cors "$BUCKET"
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
# MIN_RELEVANCE=0: Amazon Rerank 1.0 scores are not calibrated to 0..1 (measured ~0.004 for
# the right chunk), so a fixed cutoff refuses every question; the chat prompt and the
# groundedness guard handle "not in the documents". DB_CONNECT_TIMEOUT_S=10 covers a Neon
# compute waking from scale-to-zero.
function_environment() {
  jq -cn --arg bucket "$BUCKET" --arg secret_id "$SECRET_ID" --argjson extra "$1" '{Variables: ({
      ENV: "prod", SECRETS_SOURCE: "aws", SECRET_ID: $secret_id,
      LLM_PROVIDER: "anthropic", EMBEDDER_PROVIDER: "bedrock", RERANKER_PROVIDER: "bedrock",
      BLOB_BACKEND: "s3", UPLOADS_BUCKET: $bucket, EMAIL_SENDER: "resend",
      PII_ENGINE: "presidio", MIN_RELEVANCE: "0", DB_CONNECT_TIMEOUT_S: "10"} + $extra)}'
}

# ensure_function <name> <timeout seconds> <extra env JSON object>
ensure_function() {
  local name=$1 timeout=$2 env
  env=$(function_environment "$3" | jq -c --arg rev "$SECRET_VERSION" '.Variables.CONFIG_REV = $rev')
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

ensure_oac() {
  local config
  OAC_ID=$(oac_by_name | head -n 1)
  if [[ -z "$OAC_ID" ]]; then
    log "creating CloudFront origin access control $OAC_NAME"
    config=$(jq -cn --arg name "$OAC_NAME" '{Name: $name, Description: "jobmatch-ai site bucket",
      SigningProtocol: "sigv4", SigningBehavior: "always", OriginAccessControlOriginType: "s3"}')
    OAC_ID=$(awse cloudfront create-origin-access-control --origin-access-control-config \
      "$config" --query OriginAccessControl.Id --output text)
    state_add cloudfront_oac "$OAC_ID"
  fi
}

# The viewer-request function that maps /dir/ to /dir/index.html (published every deploy).
ensure_site_function() {
  local etag config
  config=$(jq -cn '{Comment: "jobmatch-ai: /dir/ -> /dir/index.html",
    Runtime: "cloudfront-js-2.0"}')
  if resource_exists awse cloudfront describe-function --name "$SITE_FUNCTION"; then
    etag=$(awse cloudfront describe-function --name "$SITE_FUNCTION" --query ETag --output text)
    etag=$(awse cloudfront update-function --name "$SITE_FUNCTION" --if-match "$etag" \
      --function-config "$config" --function-code "fileb://$HERE/index-rewrite.js" \
      --query ETag --output text)
  else
    log "creating CloudFront Function $SITE_FUNCTION"
    etag=$(awse cloudfront create-function --name "$SITE_FUNCTION" --function-config "$config" \
      --function-code "fileb://$HERE/index-rewrite.js" --query ETag --output text)
    state_add cloudfront_function "$SITE_FUNCTION"
  fi
  SITE_FUNCTION_ARN=$(awse cloudfront publish-function --name "$SITE_FUNCTION" --if-match "$etag" \
    --query FunctionSummary.FunctionMetadata.FunctionARN --output text)
}

# A recorded certificate that still exists, else one for $DOMAIN tagged as ours (a lost state
# file), else nothing.
find_certificate() {
  local arn arns
  for arn in $(state_ids acm_certificate); do
    if resource_exists awse acm describe-certificate --certificate-arn "$arn"; then
      printf '%s' "$arn"
      return 0
    fi
  done
  # A failed listing is not "no certificate": requesting another would leave a duplicate.
  arns=$(certificates_for_domain) || die "could not list ACM certificates in $EDGE_REGION"
  for arn in $arns; do
    if certificate_is_ours "$arn"; then
      state_add acm_certificate "$arn"
      printf '%s' "$arn"
      return 0
    fi
  done
}

# The DNS validation record ACM wants; it appears a few seconds after the request.
validation_record() {
  local attempt record
  for ((attempt = 1; attempt <= RETRY_ATTEMPTS; attempt++)); do
    record=$(awse acm describe-certificate --certificate-arn "$1" \
      --query 'Certificate.DomainValidationOptions[0].ResourceRecord' --output json)
    if [[ "$record" != null ]]; then
      printf '%s' "$record"
      return 0
    fi
    sleep "$RETRY_SLEEP"
  done
  die "ACM has not provided the DNS validation record for $1; re-run make deploy"
}

# Certificate in us-east-1 (CloudFront's region) with its DNS validation record UPSERTed into
# the zone. The record stays while the certificate exists: ACM renews through it.
ensure_certificate() {
  local record name type rrs
  CERT_ARN=$(find_certificate)
  if [[ -z "$CERT_ARN" ]]; then
    log "requesting an ACM certificate for $DOMAIN in $EDGE_REGION (DNS validation)"
    CERT_ARN=$(awse acm request-certificate --domain-name "$DOMAIN" --validation-method DNS \
      --tags "$TAG_KV" --query CertificateArn --output text)
    state_add acm_certificate "$CERT_ARN"
  fi
  record=$(validation_record "$CERT_ARN")
  name=$(jq -r .Name <<<"$record")
  type=$(jq -r .Type <<<"$record")
  record_allowed "$name" "$type" ||
    die "ACM asked for validation record $name $type, which is outside $DOMAIN; not writing it"
  rrs=$(jq -c '{Name, Type, TTL: 300, ResourceRecords: [{Value}]}' <<<"$record")
  log "writing the certificate's DNS validation record $name"
  record_change "$ZONE_ID" UPSERT "$rrs" "$WORK_DIR" ||
    die "could not write the DNS validation record $name"
  state_add route53_record "$name|$type"
}

wait_for_certificate() {
  local status deadline=$((SECONDS + ACM_TIMEOUT_S))
  log "waiting for the certificate to be validated (usually a few minutes)"
  while :; do
    status=$(awse acm describe-certificate --certificate-arn "$CERT_ARN" \
      --query Certificate.Status --output text)
    case "$status" in
      ISSUED) return 0 ;;
      PENDING_VALIDATION) ;;
      *) die "certificate $CERT_ARN is $status; run make teardown, then make deploy" ;;
    esac
    ((SECONDS < deadline)) ||
      die "certificate $CERT_ARN was not validated within $((ACM_TIMEOUT_S / 60)) minutes." \
        "Its DNS validation record is in place and everything so far is recorded: re-run" \
        "make deploy to keep waiting, or make teardown to remove it all."
    sleep "$ACM_POLL_SLEEP"
  done
}

# distribution_config <caller reference>: the full DistributionConfig JSON. It contains the
# origin token (read from the secret file), so it only ever goes to a file in WORK_DIR.
distribution_config() {
  local api_host=${FUNCTION_URL#https://}
  api_host=${api_host%%/*}
  jq -n --arg ref "$1" --slurpfile secret "$SECRET_FILE" --arg domain "$DOMAIN" \
    --arg comment "$DISTRIBUTION_COMMENT" --arg site "$SITE_BUCKET.s3.$REGION.amazonaws.com" \
    --arg api "$api_host" --arg oac "$OAC_ID" --arg fn "$SITE_FUNCTION_ARN" \
    --arg cert "$CERT_ARN" --arg optimized "$CACHE_POLICY_CACHING_OPTIMIZED" \
    --arg disabled "$CACHE_POLICY_CACHING_DISABLED" \
    --arg security_headers "$RESPONSE_HEADERS_POLICY_SECURITY_HEADERS" \
    --arg forward "$ORIGIN_REQUEST_POLICY_ALL_VIEWER_EXCEPT_HOST" \
    --argjson read_timeout "$ORIGIN_READ_TIMEOUT_S" '
    def list($items): {Quantity: ($items | length), Items: $items};
    ($secret[0].ORIGIN_VERIFY_TOKEN // "") as $token
    | if ($token | length) < 32 then error("ORIGIN_VERIFY_TOKEN is missing from the secret")
      else . end
    | {
      CallerReference: $ref,
      Comment: $comment,
      Enabled: true,
      Aliases: list([$domain]),
      DefaultRootObject: "index.html",
      PriceClass: "PriceClass_100",
      HttpVersion: "http2and3",
      IsIPV6Enabled: true,
      ViewerCertificate: {ACMCertificateArn: $cert, SSLSupportMethod: "sni-only",
                          MinimumProtocolVersion: "TLSv1.2_2021"},
      Origins: list([
        {Id: "site", DomainName: $site, OriginPath: "", CustomHeaders: list([]),
         S3OriginConfig: {OriginAccessIdentity: ""}, OriginAccessControlId: $oac},
        {Id: "api", DomainName: $api, OriginPath: "",
         CustomHeaders: list([{HeaderName: "X-Origin-Verify", HeaderValue: $token}]),
         CustomOriginConfig: {HTTPPort: 80, HTTPSPort: 443, OriginProtocolPolicy: "https-only",
                              OriginSslProtocols: list(["TLSv1.2"]),
                              OriginReadTimeout: $read_timeout, OriginKeepaliveTimeout: 5}}
      ]),
      DefaultCacheBehavior: {
        TargetOriginId: "site", ViewerProtocolPolicy: "redirect-to-https", Compress: true,
        CachePolicyId: $optimized, ResponseHeadersPolicyId: $security_headers,
        AllowedMethods: (list(["GET", "HEAD"]) + {CachedMethods: list(["GET", "HEAD"])}),
        FunctionAssociations: list([{FunctionARN: $fn, EventType: "viewer-request"}])
      },
      CacheBehaviors: list([{
        PathPattern: "/api/*", TargetOriginId: "api", ViewerProtocolPolicy: "https-only",
        Compress: false, CachePolicyId: $disabled, OriginRequestPolicyId: $forward,
        AllowedMethods: (list(["GET", "HEAD", "OPTIONS", "PUT", "PATCH", "POST", "DELETE"])
                         + {CachedMethods: list(["GET", "HEAD"])})
      }]),
      # S3 (through the OAC) answers 403 for a missing key. The API never answers 403 through
      # CloudFront (CloudFront always sends the origin token), so this only maps site misses.
      CustomErrorResponses: list([{ErrorCode: 403, ResponsePagePath: "/404.html",
                                   ResponseCode: "404", ErrorCachingMinTTL: 10}])
    }'
}

# A recorded distribution that still exists, else the one with our comment, else nothing.
find_distribution() {
  local id
  for id in $(state_ids cloudfront_distribution); do
    if resource_exists awse cloudfront get-distribution --id "$id"; then
      printf '%s' "$id"
      return 0
    fi
  done
  distributions_by_comment | head -n 1
}

ensure_distribution() {
  local existing etag caller_ref info
  local current="$WORK_DIR/distribution-current.json" config="$WORK_DIR/distribution.json"
  existing=$(find_distribution)
  if [[ -n "$existing" ]]; then
    awse cloudfront get-distribution-config --id "$existing" --output json >"$current"
    etag=$(jq -r .ETag "$current")
    caller_ref=$(jq -r .DistributionConfig.CallerReference "$current")
    # UpdateDistribution needs the complete config, including fields CloudFront filled in on
    # create (e.g. Logging), so lay our settings over the current config instead of replacing it.
    # Lists are replaced, not merged, by `*`, so merge each origin (by Id) and cache behaviour
    # (by PathPattern) with its current counterpart too (e.g. SmoothStreaming, OriginShield).
    distribution_config "$caller_ref" |
      jq --slurpfile current "$current" '
        $current[0].DistributionConfig as $cur
        | . as $want
        | ($cur * $want)
        | .Origins.Items = [$want.Origins.Items[] as $o
            | ((($cur.Origins.Items // [])[] | select(.Id == $o.Id)) // {}) * $o]
        | if $want.CacheBehaviors then
            .CacheBehaviors.Items = [($want.CacheBehaviors.Items // [])[] as $b
              | ((($cur.CacheBehaviors.Items // [])[] | select(.PathPattern == $b.PathPattern))
                 // {}) * $b]
          else . end' >"$config"
    log "updating CloudFront distribution $existing"
    awse cloudfront update-distribution --id "$existing" --if-match "$etag" \
      --distribution-config "file://$config" >/dev/null
    DISTRIBUTION_ID=$existing
  else
    distribution_config "jobmatch-ai-$(date -u +%Y%m%dT%H%M%SZ)" |
      jq --arg project "$PROJECT" \
        '{DistributionConfig: ., Tags: {Items: [{Key: "Project", Value: $project}]}}' >"$config"
    log "creating CloudFront distribution for $DOMAIN"
    DISTRIBUTION_ID=$(awse cloudfront create-distribution-with-tags \
      --distribution-config-with-tags "file://$config" --query Distribution.Id --output text)
    state_add cloudfront_distribution "$DISTRIBUTION_ID"
  fi
  rm -f "$config" "$current"
  info=$(awse cloudfront get-distribution --id "$DISTRIBUTION_ID" \
    --query '[Distribution.ARN, Distribution.DomainName]' --output text)
  read -r DISTRIBUTION_ARN DISTRIBUTION_DOMAIN <<<"$info"
}

# Only this distribution (through the OAC) may read the private site bucket.
ensure_site_bucket_policy() {
  awsr s3api put-bucket-policy --bucket "$SITE_BUCKET" \
    --policy "$(render_policy site-bucket-policy.json)"
}

ensure_alias_records() {
  local type current rrs
  for type in A AAAA; do
    current=$(record_get "$ZONE_ID" "$DOMAIN." "$type") || die "could not read the $DOMAIN records"
    foreign_record_guard "$type" "$current"
    rrs=$(jq -cn --arg name "$DOMAIN." --arg type "$type" --arg zone "$CLOUDFRONT_ZONE_ID" \
      --arg target "$DISTRIBUTION_DOMAIN." '{Name: $name, Type: $type,
        AliasTarget: {HostedZoneId: $zone, DNSName: $target, EvaluateTargetHealth: false}}')
    log "pointing $DOMAIN ($type) at the distribution"
    record_change "$ZONE_ID" UPSERT "$rrs" "$WORK_DIR" || die "could not write the $DOMAIN $type record"
    state_add route53_record "$DOMAIN.|$type"
  done
}

main() {
  log "deploying to account $ACCOUNT_ID, region $REGION; state file $STATE_FILE"
  check_prerequisites
  ensure_budgets
  ensure_origin_verify_token
  read_secret_version
  # Requested early: DNS validation runs while the image builds.
  ensure_certificate
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
  ensure_private_bucket "$SITE_BUCKET"
  ensure_oac
  ensure_site_function
  wait_for_certificate
  ensure_distribution
  ensure_site_bucket_policy
  ensure_alias_records
  log "deployed image $IMAGE_TAG"
  log "Function URL: $FUNCTION_URL"
  log "site: https://$DOMAIN (CloudFront $DISTRIBUTION_ID; first deploys take a few minutes)"
  log "next: make migrate-prod (first deploy or new migrations), make deploy-frontend," \
    "then make smoke-prod"
}

main "$@"
