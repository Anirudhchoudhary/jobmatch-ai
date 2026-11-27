# shellcheck shell=bash
# Helpers shared by deploy.sh and teardown.sh. Source config.sh first.
# Rules: never print a secret value, never enable xtrace.

export AWS_PAGER=""

log() { printf '==> %s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

require_tools() {
  local tool
  for tool in "$@"; do
    command -v "$tool" >/dev/null 2>&1 || die "$tool is required but not on PATH"
  done
}

# AWS CLI in the project region / the Budgets region / us-east-1 for CloudFront, Route 53, ACM.
awsr() { aws "$@" --region "$REGION"; }
awsb() { aws "$@" --region "$BUDGETS_REGION"; }
awse() { aws "$@" --region "$EDGE_REGION"; }

# True when an AWS CLI error message means the resource does not exist.
is_not_found() {
  grep -qE 'NotFound|NoSuch[A-Za-z]+|\(404\)|does not exist|cannot be found' <<<"$1"
}

# resource_exists <cmd...>: 0 if the describe/get command succeeds, 1 if the resource is
# missing; any other failure (credentials, permissions, throttling) aborts.
resource_exists() {
  local err
  if err=$("$@" 2>&1 >/dev/null); then
    return 0
  fi
  if is_not_found "$err"; then
    return 1
  fi
  die "'$1 $2 $3' failed: $err"
}

# Replace __PLACEHOLDER__ tokens in a policy template with deployment values.
render_policy() {
  sed -e "s|__REGION__|$REGION|g" \
    -e "s|__ACCOUNT_ID__|$ACCOUNT_ID|g" \
    -e "s|__BUCKET__|$BUCKET|g" \
    -e "s|__SECRET_ID__|$SECRET_ID|g" \
    -e "s|__API_FUNCTION__|$API_FUNCTION|g" \
    -e "s|__CLEANUP_FUNCTION__|$CLEANUP_FUNCTION|g" \
    -e "s|__DOMAIN__|$DOMAIN|g" \
    -e "s|__SITE_BUCKET__|${SITE_BUCKET:-}|g" \
    -e "s|__DISTRIBUTION_ARN__|${DISTRIBUTION_ARN:-}|g" \
    "$POLICY_DIR/$1"
}

# ---- Route 53: only ever the jobmatch.ntrye.com records, in the zone found by name.
# Record ids in the state file are "<name>|<type>", e.g. "jobmatch.ntrye.com.|A".

# record_allowed <name> <type>: the only record sets any script may change: the site's
# A/AAAA alias and an ACM validation CNAME directly under the site's name.
record_allowed() {
  local name=$1 type=$2 validation="^_[a-z0-9]+\.${DOMAIN//./\\.}\.\$"
  if [[ "$name" == "$DOMAIN." && ("$type" == A || "$type" == AAAA) ]]; then
    return 0
  fi
  [[ "$type" == CNAME && "$name" =~ $validation ]]
}

# hosted_zone_id: id of the public zone named $HOSTED_ZONE (empty when there is none);
# fails when the lookup fails or the name is ambiguous. Never creates a zone.
hosted_zone_id() {
  local listing ids
  listing=$(awse route53 list-hosted-zones-by-name --dns-name "$HOSTED_ZONE" --output json) ||
    return 1
  ids=$(jq -r --arg n "$HOSTED_ZONE" '.HostedZones[]?
    | select(.Name == $n and (.Config.PrivateZone | not)) | .Id | sub("^/hostedzone/"; "")' \
    <<<"$listing")
  if (($(wc -w <<<"$ids") > 1)); then
    printf 'more than one public hosted zone is named %s\n' "$HOSTED_ZONE" >&2
    return 1
  fi
  printf '%s' "$ids"
}

# record_get <zone id> <name> <type>: the record set exactly as Route 53 holds it, as compact
# JSON, or nothing when there is none.
record_get() {
  local listing
  listing=$(awse route53 list-resource-record-sets --hosted-zone-id "$1" \
    --start-record-name "$2" --start-record-type "$3" --max-items 1 --output json) || return 1
  jq -c --arg n "$2" --arg t "$3" '.ResourceRecordSets[]? | select(.Name == $n and .Type == $t)' \
    <<<"$listing"
}

# record_change <zone id> <UPSERT|DELETE> <record set JSON> <work dir>: the one place that
# changes DNS. Refuses any record set record_allowed does not allow.
record_change() {
  local zone=$1 action=$2 rrs=$3 name type batch
  name=$(jq -r .Name <<<"$rrs")
  type=$(jq -r .Type <<<"$rrs")
  if ! record_allowed "$name" "$type"; then
    printf 'refusing to %s record %s %s: only the %s records may be changed\n' \
      "$action" "$name" "$type" "$DOMAIN" >&2
    return 1
  fi
  batch=$(mktemp "$4/change-batch.XXXXXX")
  jq -n --arg a "$action" --argjson r "$rrs" \
    '{Comment: "jobmatch-ai deploy", Changes: [{Action: $a, ResourceRecordSet: $r}]}' >"$batch"
  if ! awse route53 change-resource-record-sets --hosted-zone-id "$zone" \
    --change-batch "file://$batch" >/dev/null; then
    rm -f "$batch"
    return 1
  fi
  rm -f "$batch"
}

# ---- state file: {"resources": [{"type", "id"}...], "function_url": "..."}
# Written atomically (temp file + mv) so an interrupted run never leaves a torn file.

state_write() {
  local filter=$1
  shift
  mkdir -p "$(dirname "$STATE_FILE")"
  [[ -f "$STATE_FILE" ]] || printf '{"resources": []}\n' >"$STATE_FILE"
  local tmp
  tmp=$(mktemp "$STATE_FILE.XXXXXX")
  if ! jq "$@" "$filter" "$STATE_FILE" >"$tmp"; then
    rm -f "$tmp"
    die "could not update $STATE_FILE"
  fi
  mv "$tmp" "$STATE_FILE"
}

# state_add <type> <id>: record a created resource (once).
# shellcheck disable=SC2016  # $t/$i/$u are jq variables
state_add() {
  state_write '.resources |= (if any(.type == $t and .id == $i) then . else . + [{type: $t, id: $i}] end)' \
    --arg t "$1" --arg i "$2"
}

# shellcheck disable=SC2016
state_set_url() {
  state_write '.function_url = $u' --arg u "$1"
}

# state_ids <type>: ids recorded for a type, newest first (reverse creation order).
state_ids() {
  [[ -f "$STATE_FILE" ]] || return 0
  jq -r --arg t "$1" '[.resources[] | select(.type == $t) | .id] | reverse | .[]' "$STATE_FILE"
}

# Types recorded in the state file that teardown does not know how to delete.
state_unknown_types() {
  [[ -f "$STATE_FILE" ]] || return 0
  jq -r '.resources[].type | select(IN($ARGS.positional[]) | not)' --args "$@" <"$STATE_FILE" |
    sort -u
}

# ---- CloudFront and ACM lookups by name (a lost state file must not hide a resource)

# Certificate ARNs in ACM us-east-1 for $DOMAIN (anyone's: check certificate_is_ours).
certificates_for_domain() {
  awse acm list-certificates \
    --query "CertificateSummaryList[?DomainName=='$DOMAIN'].CertificateArn" --output text |
    tr '\t' '\n' | awk 'NF && $0 != "None"'
}

# True when the certificate carries our Project tag, i.e. deploy requested it.
certificate_is_ours() {
  local tag
  tag=$(awse acm list-tags-for-certificate --certificate-arn "$1" \
    --query "Tags[?Key=='Project'].Value | [0]" --output text) || return 1
  [[ "$tag" == "$PROJECT" ]]
}

# Ids of distributions whose comment is ours.
distributions_by_comment() {
  awse cloudfront list-distributions \
    --query "DistributionList.Items[?Comment=='$DISTRIBUTION_COMMENT'].Id" --output text |
    tr '\t' '\n' | awk 'NF && $0 != "None"'
}

# Id of the origin access control named $OAC_NAME, or nothing.
oac_by_name() {
  awse cloudfront list-origin-access-controls \
    --query "OriginAccessControlList.Items[?Name=='$OAC_NAME'].Id" --output text |
    tr '\t' '\n' | awk 'NF && $0 != "None"'
}
