#!/usr/bin/env bash
# shellcheck source-path=SCRIPTDIR
# Remove everything deploy.sh created, so nothing keeps charging. Run via `make teardown`.
#
# Deletes in reverse dependency order every resource in the state file plus every name in
# config.sh (so a lost or partial state file still cleans up), treating "not found" as done.
# Keeps going past failures, then verifies by name and via the Resource Groups Tagging API
# and exits 1 listing anything left. The state file is removed only when nothing is left.
# The budget alerts are deleted last, and kept while anything else is left.
# Route 53: the ntrye.com. zone is the author's and is never deleted or changed beyond the
# jobmatch.ntrye.com A/AAAA alias and the certificate's validation CNAME, each deleted with
# a DELETE of exactly the record set Route 53 currently holds.
#
#   --keep-secret   keep the jobmatch/prod secret (default: delete it without recovery)
#   --yes           do not ask before deleting the secret
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=config.sh
source "$HERE/config.sh"
# shellcheck source=lib.sh
source "$HERE/lib.sh"

KEEP_SECRET=false
ASSUME_YES=false
for arg in "$@"; do
  case "$arg" in
    --keep-secret) KEEP_SECRET=true ;;
    --yes) ASSUME_YES=true ;;
    -h | --help)
      sed -n '2,15p' "${BASH_SOURCE[0]}"
      exit 0
      ;;
    *) die "unknown argument: $arg" ;;
  esac
done

VERIFY_SLEEP="${TEARDOWN_VERIFY_SLEEP:-20}"
VERIFY_ATTEMPTS=6
KNOWN_TYPES=(schedule iam_role_policy iam_role lambda_function function_url lambda_permission
  log_group s3_bucket ecr_repository budget s3_bucket_cors acm_certificate cloudfront_oac
  cloudfront_function cloudfront_distribution route53_record)

require_tools aws jq
ACCOUNT_ID="$(awsr sts get-caller-identity --query Account --output text)"
BUCKET="$BUCKET_PREFIX-$ACCOUNT_ID"
SITE_BUCKET="$SITE_BUCKET_PREFIX-$ACCOUNT_ID"
FAILURES=()
umask 077 # distribution configs read back from CloudFront hold the origin token
WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT
ZONE_ID=""
CERTIFICATES=()       # ours: recorded, or for $DOMAIN with our Project tag
DNS_RECORDS=()        # "<name>|<type>": the site alias records, then validation CNAMEs

# An unreadable state file must not stop the sweep by name.
RECORDED_STATE="$STATE_FILE"
if [[ -f "$STATE_FILE" ]] && ! jq -e '.resources | arrays' "$STATE_FILE" >/dev/null 2>&1; then
  warn "state file $STATE_FILE is unreadable; sweeping by the names in config.sh only"
  STATE_FILE="$WORK_DIR/no-state.json"
fi

# ids <type> <config names...>: ids from the state file (newest first), then the configured
# names, without duplicates.
ids() {
  local type=$1
  shift
  {
    state_ids "$type" || true
    printf '%s\n' "$@"
  } | awk 'NF && !seen[$0]++'
}

# try_delete <description> <cmd...>: run a delete; "not found" counts as success.
try_delete() {
  local desc=$1 err
  shift
  if err=$("$@" 2>&1 >/dev/null); then
    log "deleted $desc"
  elif is_not_found "$err"; then
    log "already gone: $desc"
  else
    warn "could not delete $desc: $err"
    FAILURES+=("$desc")
  fi
}

confirm_secret_deletion() {
  if $KEEP_SECRET || $ASSUME_YES; then
    return 0
  fi
  local answer=""
  printf 'Type %s to permanently delete that secret (anything else keeps it): ' "$SECRET_ID"
  read -r answer || true
  if [[ "$answer" != "$SECRET_ID" ]]; then
    log "not confirmed: the secret will be kept"
    KEEP_SECRET=true
    SECRET_UNCONFIRMED=true
  fi
}

delete_schedules() {
  local name names
  mapfile -t names < <(ids schedule "$SCHEDULE_NAME")
  for name in "${names[@]}"; do
    try_delete "schedule $name" awsr scheduler delete-schedule --name "$name"
  done
}

# Inline policies (state + live listing) and attached policies go before the role itself.
delete_role() {
  local role=$1 entry policy listing policies=() attached=()
  for entry in $(state_ids iam_role_policy); do
    [[ "${entry%%/*}" == "$role" ]] && policies+=("${entry#*/}")
  done
  if listing=$(awsr iam list-role-policies --role-name "$role" --query PolicyNames --output text 2>&1); then
    read -r -a attached <<<"$listing"
    policies+=("${attached[@]}")
  elif ! is_not_found "$listing"; then
    warn "could not list inline policies of $role: $listing"
  fi
  for policy in $(printf '%s\n' "${policies[@]}" | awk 'NF && $0 != "None" && !seen[$0]++'); do
    try_delete "inline policy $role/$policy" \
      awsr iam delete-role-policy --role-name "$role" --policy-name "$policy"
  done
  if listing=$(awsr iam list-attached-role-policies --role-name "$role" \
    --query 'AttachedPolicies[].PolicyArn' --output text 2>/dev/null); then
    read -r -a attached <<<"$listing"
    for policy in "${attached[@]}"; do
      [[ "$policy" == None ]] && continue
      try_delete "attached policy $policy from $role" \
        awsr iam detach-role-policy --role-name "$role" --policy-arn "$policy"
    done
  fi
  try_delete "IAM role $role" awsr iam delete-role --role-name "$role"
}

delete_function_url_and_permissions() {
  local fn entry names
  mapfile -t names < <(ids function_url "$API_FUNCTION")
  for fn in "${names[@]}"; do
    try_delete "Function URL of $fn" awsr lambda delete-function-url-config --function-name "$fn"
  done
  mapfile -t names < <(ids lambda_permission \
    "$API_FUNCTION/$URL_INVOKE_URL_SID" "$API_FUNCTION/$URL_INVOKE_FUNCTION_SID")
  for entry in "${names[@]}"; do
    try_delete "permission $entry" awsr lambda remove-permission \
      --function-name "${entry%%/*}" --statement-id "${entry#*/}"
  done
}

delete_functions() {
  local fn names
  mapfile -t names < <(ids lambda_function "$@")
  for fn in "${names[@]}"; do
    try_delete "Lambda function $fn" awsr lambda delete-function --function-name "$fn"
  done
}

# Remove every object version and delete marker, then the bucket.
empty_and_delete_bucket() {
  local bucket=$1 pass listing count batch="$WORK_DIR/delete-batch.json"
  for ((pass = 1; pass <= 20; pass++)); do
    if ! listing=$(awsr s3api list-object-versions --bucket "$bucket" --output json 2>"$WORK_DIR/err"); then
      listing=$(<"$WORK_DIR/err")
      if is_not_found "$listing"; then
        log "already gone: S3 bucket $bucket"
        return 0
      fi
      warn "could not list objects in $bucket: $listing"
      FAILURES+=("S3 bucket $bucket")
      return 0
    fi
    count=$(jq '[(.Versions // [])[], (.DeleteMarkers // [])[]] | length' <<<"$listing")
    ((${count:-0} > 0)) || break
    jq -c '{Objects: ([(.Versions // [])[], (.DeleteMarkers // [])[] | {Key, VersionId}][:1000]),
            Quiet: true}' <<<"$listing" >"$batch"
    log "deleting $((count > 1000 ? 1000 : count)) of $count object versions from $bucket"
    if ! awsr s3api delete-objects --bucket "$bucket" --delete "file://$batch" >/dev/null; then
      warn "could not delete objects from $bucket"
      FAILURES+=("objects in S3 bucket $bucket")
      break
    fi
  done
  try_delete "S3 bucket $bucket" awsr s3api delete-bucket --bucket "$bucket"
}

# ---- CloudFront, ACM and the jobmatch.ntrye.com records

# Fill CERTIFICATES, DNS_RECORDS and ZONE_ID. The validation record names come from the
# state file and from each certificate (read before it is deleted).
find_edge_resources() {
  local arn listing entry
  mapfile -t CERTIFICATES < <(state_ids acm_certificate)
  if listing=$(certificates_for_domain 2>&1); then
    for arn in $listing; do
      certificate_is_ours "$arn" && CERTIFICATES+=("$arn")
    done
  else
    warn "could not list ACM certificates for $DOMAIN: $listing"
    FAILURES+=("ACM certificates for $DOMAIN (could not list)")
  fi
  mapfile -t CERTIFICATES < <(printf '%s\n' "${CERTIFICATES[@]}" | awk 'NF && !seen[$0]++')
  local validation=()
  for arn in "${CERTIFICATES[@]}"; do
    if entry=$(awse acm describe-certificate --certificate-arn "$arn" --query \
      'Certificate.DomainValidationOptions[0].ResourceRecord.[Name, Type]' --output text 2>/dev/null) &&
      [[ "$entry" != None* ]]; then
      validation+=("${entry//$'\t'/|}")
    fi
  done
  mapfile -t DNS_RECORDS < <(ids route53_record "$DOMAIN.|A" "$DOMAIN.|AAAA" "${validation[@]}")
  if ! ZONE_ID=$(hosted_zone_id 2>&1); then
    warn "could not look up the Route 53 zone $HOSTED_ZONE: $ZONE_ID"
    FAILURES+=("DNS records for $DOMAIN (zone lookup failed)")
    ZONE_ID=""
  elif [[ -z "$ZONE_ID" ]]; then
    log "no public hosted zone $HOSTED_ZONE: no DNS records to remove"
  fi
}

# edge_ids <state type> <list function>: recorded ids, then the ones listed by name, without
# duplicates. Returns 1 (after printing what it has) when the listing failed.
edge_ids() {
  local listed status=0
  listed=$("$2") || status=1
  {
    state_ids "$1"
    printf '%s\n' "$listed"
  } | awk 'NF && !seen[$0]++'
  return $status
}

# True when the record deploy would have made: a validation CNAME, or a CloudFront alias.
record_is_ours() {
  local type=$1 current=$2
  [[ "$type" == CNAME ]] ||
    [[ "$(jq -r '.AliasTarget.HostedZoneId // empty' <<<"$current")" == "$CLOUDFRONT_ZONE_ID" ]]
}

# delete_dns_record <name|type>: DELETE exactly the record set Route 53 holds now.
delete_dns_record() {
  local name=${1%|*} type=${1##*|} desc current
  desc="DNS record $name $type"
  [[ -n "$ZONE_ID" ]] || return 0
  if ! record_allowed "$name" "$type"; then
    warn "refusing to touch $desc: only the $DOMAIN records are deploy's"
    FAILURES+=("$desc (outside $DOMAIN; not touched)")
    return 0
  fi
  if ! current=$(record_get "$ZONE_ID" "$name" "$type" 2>&1); then
    warn "could not read $desc: $current"
    FAILURES+=("$desc")
    return 0
  fi
  if [[ -z "$current" ]]; then
    log "already gone: $desc"
  elif ! record_is_ours "$type" "$current"; then
    log "note: $desc is not a CloudFront alias, so deploy did not create it; left alone"
  else
    try_delete "$desc" record_change "$ZONE_ID" DELETE "$current" "$WORK_DIR"
  fi
}

# Disable, wait until deployed, then delete with the current ETag.
delete_distribution() {
  local id=$1 desc="CloudFront distribution $1" config="$WORK_DIR/distribution-$1.json" etag err
  if ! awse cloudfront get-distribution-config --id "$id" --output json >"$config" 2>"$WORK_DIR/err"; then
    err=$(<"$WORK_DIR/err")
    rm -f "$config"
    if is_not_found "$err"; then
      log "already gone: $desc"
    else
      warn "could not read $desc: $err"
      FAILURES+=("$desc")
    fi
    return 0
  fi
  if [[ "$(jq -r .DistributionConfig.Enabled "$config")" == true ]]; then
    etag=$(jq -r .ETag "$config")
    jq '.DistributionConfig | .Enabled = false' "$config" >"$config.disabled"
    log "disabling $desc"
    if ! err=$(awse cloudfront update-distribution --id "$id" --if-match "$etag" \
      --distribution-config "file://$config.disabled" 2>&1 >/dev/null); then
      rm -f "$config" "$config.disabled"
      warn "could not disable $desc: $err"
      FAILURES+=("$desc")
      return 0
    fi
  fi
  rm -f "$config" "$config.disabled"
  log "waiting for $desc to finish deploying (disabling often takes 15+ minutes)"
  if ! err=$(awse cloudfront wait distribution-deployed --id "$id" 2>&1); then
    warn "$desc is still deploying: $err"
    FAILURES+=("$desc (still disabling; re-run make teardown later)")
    return 0
  fi
  if ! etag=$(awse cloudfront get-distribution --id "$id" --query ETag --output text 2>&1); then
    warn "could not read $desc: $etag"
    FAILURES+=("$desc")
    return 0
  fi
  try_delete "$desc" awse cloudfront delete-distribution --id "$id" --if-match "$etag"
}

# delete_with_etag <description> <get cmd...> -- <delete cmd...>: CloudFront deletes need the
# current ETag (passed as --if-match).
delete_with_etag() {
  local desc=$1 get=() etag
  shift
  while [[ "$1" != -- ]]; do
    get+=("$1")
    shift
  done
  shift
  if ! etag=$("${get[@]}" --query ETag --output text 2>&1); then
    if is_not_found "$etag"; then
      log "already gone: $desc"
    else
      warn "could not read $desc: $etag"
      FAILURES+=("$desc")
    fi
    return 0
  fi
  try_delete "$desc" "$@" --if-match "$etag"
}

delete_site_and_edge() {
  local entry id names cert
  find_edge_resources
  # The name stops pointing at CloudFront first.
  for entry in "${DNS_RECORDS[@]}"; do
    [[ "$entry" == *"|CNAME" ]] || delete_dns_record "$entry"
  done
  edge_ids cloudfront_distribution distributions_by_comment >"$WORK_DIR/ids" ||
    FAILURES+=("CloudFront distributions (could not list)")
  mapfile -t names <"$WORK_DIR/ids"
  for id in "${names[@]}"; do
    delete_distribution "$id"
  done
  mapfile -t names < <(ids cloudfront_function "$SITE_FUNCTION")
  for id in "${names[@]}"; do
    delete_with_etag "CloudFront Function $id" awse cloudfront describe-function --name "$id" -- \
      awse cloudfront delete-function --name "$id"
  done
  edge_ids cloudfront_oac oac_by_name >"$WORK_DIR/ids" ||
    FAILURES+=("CloudFront origin access controls (could not list)")
  mapfile -t names <"$WORK_DIR/ids"
  for id in "${names[@]}"; do
    delete_with_etag "CloudFront origin access control $id" \
      awse cloudfront get-origin-access-control --id "$id" -- \
      awse cloudfront delete-origin-access-control --id "$id"
  done
  # The certificate was in use by the distribution until now.
  for entry in "${DNS_RECORDS[@]}"; do
    [[ "$entry" == *"|CNAME" ]] && delete_dns_record "$entry"
  done
  for cert in "${CERTIFICATES[@]}"; do
    try_delete "ACM certificate $cert" awse acm delete-certificate --certificate-arn "$cert"
  done
}

delete_everything() {
  local name names role
  delete_site_and_edge
  delete_schedules
  delete_role "$SCHEDULER_ROLE"
  try_delete "Lambda function $CLEANUP_FUNCTION" \
    awsr lambda delete-function --function-name "$CLEANUP_FUNCTION"
  delete_function_url_and_permissions
  delete_functions "$API_FUNCTION" "$CLEANUP_FUNCTION"
  mapfile -t names < <(ids iam_role "$LAMBDA_ROLE" "$SCHEDULER_ROLE")
  for role in "${names[@]}"; do
    [[ "$role" == "$SCHEDULER_ROLE" ]] || delete_role "$role"
  done
  mapfile -t names < <(ids log_group "/aws/lambda/$API_FUNCTION" "/aws/lambda/$CLEANUP_FUNCTION")
  for name in "${names[@]}"; do
    try_delete "log group $name" awsr logs delete-log-group --log-group-name "$name"
  done
  mapfile -t names < <(ids s3_bucket_cors "$BUCKET")
  for name in "${names[@]}"; do
    try_delete "CORS configuration of $name" awsr s3api delete-bucket-cors --bucket "$name"
  done
  mapfile -t names < <(ids s3_bucket "$BUCKET" "$SITE_BUCKET")
  for name in "${names[@]}"; do
    empty_and_delete_bucket "$name"
  done
  mapfile -t names < <(ids ecr_repository "$ECR_REPO")
  for name in "${names[@]}"; do
    try_delete "ECR repository $name (and its images)" \
      awsr ecr delete-repository --repository-name "$name" --force
  done
  if $KEEP_SECRET; then
    log "keeping secret $SECRET_ID"
  else
    try_delete "secret $SECRET_ID" awsr secretsmanager delete-secret \
      --secret-id "$SECRET_ID" --force-delete-without-recovery
  fi
}

# ---- verification

LEFTOVERS=()
declare -A CONFIRMED_GONE=() # descriptions whose by-name check found nothing

# still_there <description> <cmd...>: record the resource if the describe call finds it.
still_there() {
  local desc=$1 err
  shift
  if err=$("$@" 2>&1 >/dev/null); then
    LEFTOVERS+=("$desc")
  elif is_not_found "$err"; then
    CONFIRMED_GONE["$desc"]=1
  else
    LEFTOVERS+=("$desc (could not check: $err)")
  fi
}

# The verify_by_name description for a tagged ARN (empty for kinds it does not check).
arn_description() {
  local arn=$1 rest
  case "$arn" in
    arn:aws:lambda:*:function:*)
      rest=${arn##*:function:}
      printf 'Lambda function %s' "${rest%%:*}"
      ;;
    arn:aws:s3:::*) printf 'S3 bucket %s' "${arn#arn:aws:s3:::}" ;;
    arn:aws:logs:*:log-group:*)
      rest=${arn#*:log-group:}
      printf 'log group %s' "${rest%:\*}"
      ;;
    arn:aws:ecr:*:repository/*) printf 'ECR repository %s' "${arn##*:repository/}" ;;
    arn:aws:secretsmanager:*:secret:*)
      rest=${arn##*:secret:}
      printf 'secret %s' "${rest%-??????}"
      ;;
  esac
}

# CloudFront, ACM (us-east-1) and DNS are not in the us-west-2 tagging API: check by name.
verify_edge_by_name() {
  local name names entry current arn listing
  edge_ids cloudfront_distribution distributions_by_comment >"$WORK_DIR/ids" ||
    LEFTOVERS+=("CloudFront distributions (could not list)")
  mapfile -t names <"$WORK_DIR/ids"
  for name in "${names[@]}"; do
    still_there "CloudFront distribution $name" awse cloudfront get-distribution --id "$name"
  done
  mapfile -t names < <(ids cloudfront_function "$SITE_FUNCTION")
  for name in "${names[@]}"; do
    still_there "CloudFront Function $name" awse cloudfront describe-function --name "$name"
  done
  edge_ids cloudfront_oac oac_by_name >"$WORK_DIR/ids" ||
    LEFTOVERS+=("CloudFront origin access controls (could not list)")
  mapfile -t names <"$WORK_DIR/ids"
  for name in "${names[@]}"; do
    still_there "CloudFront origin access control $name" \
      awse cloudfront get-origin-access-control --id "$name"
  done
  names=("${CERTIFICATES[@]}")
  if listing=$(certificates_for_domain 2>&1); then
    for arn in $listing; do
      certificate_is_ours "$arn" && names+=("$arn")
    done
  else
    LEFTOVERS+=("ACM certificates for $DOMAIN (could not list: $listing)")
  fi
  for arn in $(printf '%s\n' "${names[@]}" | awk 'NF && !seen[$0]++'); do
    still_there "ACM certificate $arn" awse acm describe-certificate --certificate-arn "$arn"
  done
  [[ -n "$ZONE_ID" ]] || return 0
  for entry in "${DNS_RECORDS[@]}"; do
    if ! current=$(record_get "$ZONE_ID" "${entry%|*}" "${entry##*|}" 2>&1); then
      LEFTOVERS+=("DNS record ${entry/|/ } (could not check: $current)")
    elif [[ -n "$current" ]] && record_is_ours "${entry##*|}" "$current"; then
      LEFTOVERS+=("DNS record ${entry/|/ }")
    fi
  done
}

verify_by_name() {
  local name names found deleted
  verify_edge_by_name
  mapfile -t names < <(ids schedule "$SCHEDULE_NAME")
  for name in "${names[@]}"; do
    still_there "schedule $name" awsr scheduler get-schedule --name "$name"
  done
  mapfile -t names < <(ids iam_role "$LAMBDA_ROLE" "$SCHEDULER_ROLE")
  for name in "${names[@]}"; do
    still_there "IAM role $name" awsr iam get-role --role-name "$name"
  done
  mapfile -t names < <(ids lambda_function "$API_FUNCTION" "$CLEANUP_FUNCTION")
  for name in "${names[@]}"; do
    still_there "Lambda function $name" awsr lambda get-function --function-name "$name"
  done
  mapfile -t names < <(ids log_group "/aws/lambda/$API_FUNCTION" "/aws/lambda/$CLEANUP_FUNCTION")
  for name in "${names[@]}"; do
    if found=$(awsr logs describe-log-groups --log-group-name-prefix "$name" \
      --query "length(logGroups[?logGroupName=='$name'])" --output text 2>&1); then
      if [[ "$found" == 0 ]]; then
        CONFIRMED_GONE["log group $name"]=1
      else
        LEFTOVERS+=("log group $name")
      fi
    else
      LEFTOVERS+=("log group $name (could not check: $found)")
    fi
  done
  mapfile -t names < <(ids s3_bucket "$BUCKET" "$SITE_BUCKET")
  for name in "${names[@]}"; do
    still_there "S3 bucket $name" awsr s3api head-bucket --bucket "$name"
  done
  mapfile -t names < <(ids ecr_repository "$ECR_REPO")
  for name in "${names[@]}"; do
    still_there "ECR repository $name" awsr ecr describe-repositories --repository-names "$name"
  done
  if ! $KEEP_SECRET || [[ -n "${SECRET_UNCONFIRMED:-}" ]]; then
    # A force-deleted secret can still be described for a moment, with DeletedDate set.
    if deleted=$(awsr secretsmanager describe-secret --secret-id "$SECRET_ID" \
      --query DeletedDate --output text 2>&1); then
      if [[ "$deleted" != None ]]; then
        CONFIRMED_GONE["secret $SECRET_ID"]=1
      else
        LEFTOVERS+=("secret $SECRET_ID")
      fi
    elif is_not_found "$deleted"; then
      CONFIRMED_GONE["secret $SECRET_ID"]=1
    else
      LEFTOVERS+=("secret $SECRET_ID (could not check: $deleted)")
    fi
  fi
}

# Anything still tagged Project=jobmatch-ai in the region (IAM roles, budgets and schedules
# are not covered by this API; verify_by_name checks those). Retries for its index lag.
verify_by_tag() {
  local attempt arns tagged=() others=()
  for ((attempt = 1; attempt <= VERIFY_ATTEMPTS; attempt++)); do
    if ! arns=$(awsr resourcegroupstaggingapi get-resources \
      --tag-filters "Key=Project,Values=$PROJECT" \
      --query 'ResourceTagMappingList[].ResourceARN' --output text 2>&1); then
      LEFTOVERS+=("(tagging API query failed: $arns)")
      return 0
    fi
    mapfile -t tagged < <(tr '\t' '\n' <<<"$arns" | awk 'NF && $0 != "None"')
    # Other secrets (jobmatch/dev, jobmatch/ci) are made by hand, not by deploy: report only.
    mapfile -t others < <(printf '%s\n' "${tagged[@]}" |
      awk -v own=":secret:$SECRET_ID-" 'index($0, ":secret:") && !index($0, own)')
    mapfile -t tagged < <(printf '%s\n' "${tagged[@]}" |
      awk -v own=":secret:$SECRET_ID-" 'NF && (!index($0, ":secret:") || index($0, own))')
    if $KEEP_SECRET && [[ -z "${SECRET_UNCONFIRMED:-}" ]]; then
      mapfile -t tagged < <(printf '%s\n' "${tagged[@]}" |
        awk -v kept=":secret:$SECRET_ID-" 'NF && index($0, kept) == 0')
    fi
    if ((${#tagged[@]} == 0)); then
      break
    fi
    if ((attempt < VERIFY_ATTEMPTS)); then
      log "tagging API still lists ${#tagged[@]} resource(s); rechecking in ${VERIFY_SLEEP}s"
      sleep "$VERIFY_SLEEP"
    fi
  done
  local arn desc
  for arn in "${others[@]}"; do
    log "note: $arn is also tagged Project=$PROJECT but is not managed by deploy; left alone"
  done
  for arn in "${tagged[@]}"; do
    desc=$(arn_description "$arn")
    if [[ -n "$desc" && -n "${CONFIRMED_GONE[$desc]:-}" ]]; then
      # The tagging index lags behind deletes; the by-name check is authoritative here.
      warn "tagging API still lists $arn, but '$desc' was confirmed deleted by name" \
        "(index lag); not counted as left over"
    else
      LEFTOVERS+=("tagged $arn")
    fi
  done
}

# The budget alerts go last, and only once nothing billable is left: while something still
# costs money they are the only warning.
delete_budgets() {
  local name names
  mapfile -t names < <(ids budget "${BUDGET_NAMES[@]}")
  for name in "${names[@]}"; do
    try_delete "budget $name" awsb budgets delete-budget --account-id "$ACCOUNT_ID" --budget-name "$name"
  done
  for name in "${names[@]}"; do
    still_there "budget $name" awsb budgets describe-budget --account-id "$ACCOUNT_ID" --budget-name "$name"
  done
}

main() {
  local unknown
  log "tearing down account $ACCOUNT_ID, region $REGION; state file $STATE_FILE"
  unknown=$(state_unknown_types "${KNOWN_TYPES[@]}")
  if [[ -n "$unknown" ]]; then
    warn "state file lists resource types this script cannot delete: $unknown"
    FAILURES+=("unknown state entries: $unknown")
  fi
  confirm_secret_deletion
  delete_everything
  verify_by_name
  verify_by_tag
  if ((${#FAILURES[@]} > 0 || ${#LEFTOVERS[@]} > 0)); then
    warn "keeping the budget alerts (${BUDGET_NAMES[*]}) until everything else is gone"
  else
    delete_budgets
  fi
  if ((${#FAILURES[@]} > 0 || ${#LEFTOVERS[@]} > 0)); then
    printf '\nteardown incomplete; these may still exist and cost money:\n' >&2
    printf '  - %s\n' "${FAILURES[@]}" "${LEFTOVERS[@]}" | awk 'NF > 1 && !seen[$0]++' >&2
    printf 'state file kept at %s; fix the cause and re-run make teardown\n' "$RECORDED_STATE" >&2
    exit 1
  fi
  rm -f "$RECORDED_STATE"
  if $KEEP_SECRET; then
    log "done: everything removed except secret $SECRET_ID (kept on request)"
  else
    log "done: nothing tagged Project=$PROJECT or named in config.sh remains"
  fi
}

main
