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

# AWS CLI in the project region / the Budgets region.
awsr() { aws "$@" --region "$REGION"; }
awsb() { aws "$@" --region "$BUDGETS_REGION"; }

# True when an AWS CLI error message means the resource does not exist.
is_not_found() {
  grep -qE 'NotFound|NoSuchEntity|NoSuchBucket|\(404\)|does not exist|cannot be found' <<<"$1"
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
    "$POLICY_DIR/$1"
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
