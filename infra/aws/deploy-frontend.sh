#!/usr/bin/env bash
# shellcheck source-path=SCRIPTDIR
# Build the static frontend and publish it to the site bucket behind CloudFront. Run via
# `make deploy-frontend`, after `make deploy` has created (and recorded) the distribution.
#
# Content-hashed assets (_next/static/) are cached for a year; everything else (HTML, the
# RSC .txt payloads, favicon) is revalidated on every request, so a deploy shows up at once.
# Objects no longer in the build are deleted. Ends with a CloudFront invalidation of /*.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=config.sh
source "$HERE/config.sh"
# shellcheck source=lib.sh
source "$HERE/lib.sh"

FRONTEND_DIR="${FRONTEND_DIR:-$REPO_ROOT/frontend}"
IMMUTABLE="public, max-age=31536000, immutable"

require_tools aws jq npm

if [[ ! -f "$STATE_FILE" ]] || ! jq -e '.resources | arrays' "$STATE_FILE" >/dev/null 2>&1; then
  die "no readable state file at $STATE_FILE: run make deploy first"
fi
DISTRIBUTION_ID=$(state_ids cloudfront_distribution | head -n 1)
SITE_BUCKET=$(state_ids s3_bucket | grep -E "^$SITE_BUCKET_PREFIX-[0-9]+$" | head -n 1 || true)
[[ -n "$DISTRIBUTION_ID" ]] || die "no CloudFront distribution in $STATE_FILE: run make deploy first"
[[ -n "$SITE_BUCKET" ]] || die "no site bucket in $STATE_FILE: run make deploy first"

log "building the frontend in $FRONTEND_DIR"
(cd "$FRONTEND_DIR" && npm ci && npm run build)
OUT="$FRONTEND_DIR/out"
[[ -f "$OUT/index.html" ]] || die "the build did not produce $OUT/index.html"

log "uploading to s3://$SITE_BUCKET"
# Hashed assets first, so new HTML never references a file that is not there yet.
awsr s3 sync "$OUT/_next/static" "s3://$SITE_BUCKET/_next/static" --delete \
  --cache-control "$IMMUTABLE" --only-show-errors
awsr s3 sync "$OUT" "s3://$SITE_BUCKET" --delete --exclude "_next/static/*" \
  --cache-control no-cache --only-show-errors

INVALIDATION=$(awse cloudfront create-invalidation --distribution-id "$DISTRIBUTION_ID" \
  --paths '/*' --query Invalidation.Id --output text)
log "invalidation $INVALIDATION of /* started on $DISTRIBUTION_ID (edges refresh in a minute or two)"
log "site: https://$DOMAIN"
