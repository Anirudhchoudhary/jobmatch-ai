#!/usr/bin/env bash
# shellcheck source-path=SCRIPTDIR
# Post-deploy checks. Run via `make smoke-prod`. Reads only the state file; no AWS calls.
#
# Always: <Function URL>/health reports "status": "ok". Once deploy has recorded a CloudFront
# distribution, also: the site answers 200, /api/auth/me through CloudFront answers 401 (the
# API is reached and wants a session), and the Function URL's /api/auth/me answers 403 (the
# origin check refuses requests that did not come through CloudFront). Every check runs; any
# failure makes the exit status 1.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=config.sh
source "$HERE/config.sh"
# shellcheck source=lib.sh
source "$HERE/lib.sh"

require_tools curl jq

URL=$(jq -r '.function_url // empty' "$STATE_FILE" 2>/dev/null || true)
[[ -n "$URL" ]] || die "no Function URL in $STATE_FILE: run make deploy first"
URL=${URL%/}
FAILED=0

fail() {
  printf 'smoke-prod failed: %s\n' "$*" >&2
  FAILED=1
}

# expect_status <url> <status>
expect_status() {
  local code
  code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 60 "$1" 2>/dev/null) || code=000
  if [[ "$code" == "$2" ]]; then
    log "ok: $1 -> $code"
  else
    fail "$1 answered ${code:-000}, expected $2"
  fi
}

if curl -fsS --max-time 60 "$URL/health" 2>/dev/null | jq -e '.status == "ok"' >/dev/null 2>&1; then
  log "ok: $URL/health"
else
  fail "$URL/health did not report status ok"
fi

if [[ -n "$(state_ids cloudfront_distribution | head -n 1)" ]]; then
  expect_status "https://$DOMAIN/" 200
  expect_status "https://$DOMAIN/api/auth/me" 401
  expect_status "$URL/api/auth/me" 403
else
  log "no CloudFront distribution recorded: checked the Function URL only"
fi

((FAILED == 0)) || exit 1
log "smoke-prod ok"
