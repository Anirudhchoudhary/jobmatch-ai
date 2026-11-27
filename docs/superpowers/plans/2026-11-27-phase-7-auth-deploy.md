# Phase 7: Email OTP Sign-in, S3 Uploads, Lambda Packaging and AWS CLI Deploy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the API safe to expose (email one-time-code sign-in with signed session cookies), able to run on Lambda (S3 blob store, Lambda Web Adapter image, 72-hour cleanup), and deployable/removable with idempotent AWS CLI scripts that track every resource they create.

**Architecture:** Auth is a small `app/auth/` package (code hashing, session signing, email senders) plus `/api/auth/*` routes; `get_current_user_id` keeps its signature and reads the cookie. Uploads switch to S3 presigned PUTs behind the existing `BlobStore` protocol. One container image (FastAPI + Lambda Web Adapter, response streaming) runs as a public Function URL; the same image with `CLEANUP_MODE=true` runs as a private cleanup function on an hourly EventBridge Scheduler. `infra/aws/deploy.sh` creates resources and records each in `var/deploy/state.json`; `infra/aws/teardown.sh` deletes everything listed there. Postgres is Neon (author-managed); secrets live in `jobmatch/prod`, filled by `make secrets-set` with hidden input.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy/Alembic, httpx (Resend REST), boto3 (S3), AWS Lambda Web Adapter, AWS CLI v2 + bash, Docker.

**Spec:** `docs/superpowers/specs/2026-11-26-jobmatch-ai-design.md` §2 (access), §6 (architecture), §7.8 (security), §7.9 (data lifecycle), §10 (infra).

## Global Constraints

- OTP (spec §2, verbatim values): normalize email; 6-digit code from a CSPRNG; store only HMAC-SHA256 hashes (`login_codes`: email hash, code hash, expires_at = now+10 min, attempts, consumed_at); one active code per email (a new request invalidates the old); response identical whether or not the email is new; constant-time compare; max 5 attempts per code; signed HTTP-only `SameSite=Lax`, `Secure` session cookie, 7-day expiry; user row created on first successful verify; code requests ≤3 per email per hour and ≤10 per IP per hour; disposable-email domains blocked with a small denylist; mail sent via Resend.
- Emails are never written to logs, traces, `request_log`, `guardrail_events` or Langfuse (traces use the user id). Only `users.email` holds the address.
- All secrets in AWS Secrets Manager `jobmatch/prod` (keys: `ANTHROPIC_API_KEY`, `DATABASE_URL`, `SESSION_SIGNING_KEY`, `OTP_HMAC_KEY`, `RESEND_API_KEY`, optional `LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY`, `EMAIL_FROM`). Nothing secret in the repo, the image, logs, script output or `var/deploy/state.json`. No `.env` baked into the image.
- Region `us-west-2`. Nothing billed per hour (no RDS, NAT, ALB, EC2, Fargate, API Gateway REST, provisioned concurrency). Expected AWS cost < $2/month.
- Lambda role least-privilege: Titan and Rerank model ARNs, `secretsmanager:GetSecretValue` on `jobmatch/prod` only, the uploads bucket prefix `uploads/*`, its own log groups.
- CloudWatch log retention 14 days. S3 uploads private, SSE, block public access, 3-day lifecycle expiry as backstop.
- Data lifecycle (spec §7.9): hourly cleanup deletes documents (cascading chunks/analyses), semantic cache rows, login codes and usage counters older than 72 hours; `users`, `guardrail_events` and `request_log` keep their non-text rows.
- AWS Budgets alerts at $10 and $25; the alert email comes from the `BUDGET_EMAIL` env var at deploy time and is never committed.
- Every AWS resource the scripts create is tagged `Project=jobmatch-ai` and recorded in the state file; teardown must remove all of them.
- Offline tests stay offline: no real AWS, Resend or Anthropic call in any test.

## Review Focus

1. Email case/whitespace variants (`" Jane@Example.com "`) must map to one account and one rate-limit bucket — test in Task 1 and Task 3.
2. A second code request must make the first code unusable even if it is correct — test in Task 3.
3. A tampered, expired or wrong-key session cookie must yield 401 in prod, never the dev user — test in Task 3.
4. A deploy that fails half-way must still leave a state file that lets teardown delete what was created — test in Task 6.
5. A presigned PUT whose body size differs from the declared size must be rejected by S3 (signed `Content-Length`) and the key must be owned by the caller — test in Task 4.

---

### Task 1: Auth primitives (code hashing, session tokens, settings, migration)

**Files:**
- Create: `backend/app/auth/__init__.py`, `backend/app/auth/codes.py`, `backend/app/auth/session.py`, `backend/migrations/versions/0008_login_codes.py`
- Modify: `backend/app/core/settings.py`, `backend/app/db/models.py`
- Test: `backend/tests/unit/test_auth_codes.py`, `backend/tests/unit/test_session_token.py`, `backend/tests/unit/test_settings.py` (extend), `backend/tests/integration/test_schema.py` (extend)

**Interfaces:**
- Produces:
  - Settings: `session_signing_key: SecretStr | None = None`, `otp_hmac_key: SecretStr | None = None`, `resend_api_key: SecretStr | None = None`, `email_from: str | None = None`, `email_sender: Literal["console", "resend"] = "console"`, `session_cookie_name: str = "jm_session"`, `session_ttl_days: int = 7`, `otp_ttl_minutes: int = 10`, `otp_max_attempts: int = 5`, `otp_per_email_per_hour: int = 3`, `otp_per_ip_per_hour: int = 10`, `blob_backend: Literal["local", "s3"] = "local"`, `uploads_bucket: str | None = None`, `cleanup_mode: bool = False`, `retention_hours: int = 72`. Validator: when `env == "prod"`: `session_signing_key`, `otp_hmac_key` required; `email_sender == "resend"` requires `resend_api_key` and `email_from`; `blob_backend == "s3"` requires `uploads_bucket`; `email_sender == "console"` is rejected in prod.
  - `LoginCode` model / table `login_codes`: `id uuid pk`, `email_hash text not null`, `ip_hash text not null`, `code_hash text not null`, `expires_at timestamptz`, `attempts int default 0`, `consumed_at timestamptz null`, `invalidated_at timestamptz null`, `created_at timestamptz default now()`; indexes on `(email_hash, created_at)` and `(ip_hash, created_at)`. Migration `0008` (down_revision `0007`).
  - `app/auth/codes.py`: `normalize_email(raw: str) -> str` (strip, lowercase; raise `ValueError` if not one `@` with non-empty parts and a dot in the domain); `hmac_hex(key: SecretStr, value: str) -> str` (HMAC-SHA256 hex); `new_code() -> str` (`secrets.randbelow(10**6)` zero-padded to 6); `codes_equal(a: str, b: str) -> bool` (`hmac.compare_digest`); `DISPOSABLE_DOMAINS: frozenset[str]` (≈20 well-known disposable domains, e.g. `mailinator.com`, `10minutemail.com`, `guerrillamail.com`, `yopmail.com`, `tempmail.com`); `is_disposable(email: str) -> bool` (domain or any parent domain in the set).
  - `app/auth/session.py`: `issue(user_id: UUID, *, key: SecretStr, ttl: timedelta, now: datetime) -> str` → `base64url(json{"uid","exp"}) + "." + base64url(hmac_sha256)`; `verify(token: str, *, key: SecretStr, now: datetime) -> UUID | None` (None on bad format, bad signature (compare_digest), expired, or bad uid; never raises).
- [ ] **Step 1: Failing tests:** `test_normalize_email_variants` (`" Jane.Doe@Example.COM "` → `"jane.doe@example.com"`; `"no-at"`, `"a@b"`, `"a@@b.com"` raise); `test_new_code_is_six_digits` (100 draws all `len == 6` and digits); `test_hmac_hex_depends_on_key`; `test_disposable_includes_subdomains` (`x@mail.mailinator.com` True, `x@example.com` False); `test_session_roundtrip`, `test_session_rejects_tampered_payload`, `test_session_rejects_wrong_key`, `test_session_rejects_expired`, `test_session_rejects_garbage` (`""`, `"a.b.c"`, non-base64); settings: `test_prod_requires_auth_keys`, `test_prod_rejects_console_sender`, `test_s3_backend_requires_bucket`; integration `test_login_codes_table_exists` with the two indexes.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement; migration mirrors the model.
- [ ] **Step 4:** Run unit + `pytest -m integration tests/integration/test_schema.py` → PASS; `make lint typecheck`.
- [ ] **Step 5:** Commit `feat(auth): OTP code hashing, signed session tokens, login_codes table`.

### Task 2: Email senders

**Files:**
- Create: `backend/app/auth/email.py`
- Test: `backend/tests/unit/test_email_sender.py`

**Interfaces:**
- Consumes: Task 1 settings.
- Produces: `class EmailSender(Protocol): async def send_code(self, to: str, code: str) -> None`; `class ResendSender` (`__init__(api_key: SecretStr, sender: str, client: httpx.AsyncClient | None = None)`; POSTs `https://api.resend.com/emails` with `Authorization: Bearer <key>`, JSON `{"from", "to": [to], "subject": "Your JobMatch AI sign-in code", "text": "Your code is <code>. It expires in 10 minutes. If you didn't ask for it, ignore this email."}`, timeout 10 s; non-2xx → raise `EmailSendError` (message contains status code only, never the address, code or key)); `class ConsoleSender` (logs `login_code_console` with the code and **no** email, for `env == "local"` only — constructor raises `RuntimeError` otherwise); `class EmailSendError(Exception)`; `def get_email_sender() -> EmailSender` (lru_cached, from settings).
- [ ] **Step 1: Failing tests** using `httpx.MockTransport`: `test_resend_posts_expected_payload` (URL, bearer header, `to` list, subject, code in text); `test_resend_error_hides_details` (500 → `EmailSendError`, `str(err)` contains `500` and none of the address, code, key); `test_console_sender_refuses_outside_local`; `test_console_sender_never_logs_email` (structlog capture: event has `code`, no field contains the address).
- [ ] **Step 2–4:** RED → implement → GREEN; lint/typecheck.
- [ ] **Step 5:** Commit `feat(auth): Resend and local console email senders`.

### Task 3: Auth API and session-based identity

**Files:**
- Create: `backend/app/api/auth.py`, `backend/app/auth/service.py`
- Modify: `backend/app/api/deps.py`, `backend/app/main.py`, `backend/app/core/errors.py` if a new error helper is needed
- Test: `backend/tests/integration/test_auth_api.py`, `backend/tests/unit/test_deps.py` (extend)

**Interfaces:**
- Consumes: Tasks 1–2.
- Produces:
  - `app/auth/service.py`: `async def request_code(session, *, email: str, ip: str, settings, sender: EmailSender, now: datetime) -> None` — normalizes; disposable → silently do nothing (same response); counts rows in the last hour for `email_hash` (≥ `otp_per_email_per_hour` → raise `AppError("too_many_requests", "Too many codes requested. Try again in an hour.", 429)`) and for `ip_hash` (≥ `otp_per_ip_per_hour` → same error); sets `invalidated_at = now` on that email's unconsumed codes; inserts the new row; then sends. A send failure logs `login_code_send_failed` (error type only) and raises `AppError("email_failed", "We couldn't send the email. Try again shortly.", 502)`.
    `async def verify_code(session, *, email: str, code: str, settings, now: datetime) -> UUID` — finds the newest code for the email hash that is not consumed, not invalidated, not expired; none → `AppError("invalid_code", "That code is invalid or expired.", 400)`; `attempts >= otp_max_attempts` → same error; increments `attempts` before comparing; mismatch → same error; match → `consumed_at = now`, upsert `users(email)` and set `last_seen_at`, return the user id.
  - Routes (`/api/auth`): `POST /request-code {email}` → `202 {"status": "sent"}` always on success paths (including disposable); `POST /verify {email, code}` → `200 {"user_id"}` + `Set-Cookie` (`HttpOnly; SameSite=Lax; Secure` unless `env == "local"`; `Max-Age` = 7 days; `Path=/`); `POST /logout` → clears the cookie, 204; `GET /me` → `{"user_id", "email"}` for a valid session else 401. Client IP: first entry of `X-Forwarded-For`, else `request.client.host`; only its HMAC is stored.
  - `get_current_user_id`: valid cookie → that user id (and sets `usage.user_id`); no/invalid cookie → dev user when `env in ("local", "ci")`, else `AppError("auth_required", "Sign in to continue.", 401)`. Signature unchanged.
- [ ] **Step 1: Failing integration tests** (fake sender recording `(to, code)`, `env` local unless stated): `test_request_then_verify_sets_cookie_and_creates_user`; `test_email_variants_share_account` (`" A@Example.com "` then `"a@example.com"` → same user id); `test_new_request_invalidates_old_code`; `test_five_wrong_attempts_lock_code` (6th try with the right code → 400); `test_expired_code_rejected` (freeze `now` +11 min); `test_per_email_limit` (4th request in an hour → 429), `test_per_ip_limit` (11 requests from one IP across emails → 429); `test_disposable_gets_same_response_and_no_email`; `test_unknown_and_known_email_same_response`; `test_prod_requires_cookie` (env prod: no cookie → 401; tampered cookie → 401; valid cookie → that user's documents only); `test_me_and_logout`; `test_email_never_logged` (capture rendered logs during the flow; the address never appears). Unit: `test_deps` dev fallback only in local/ci.
- [ ] **Step 2–4:** RED → implement → GREEN; full unit + integration suites green; lint/typecheck.
- [ ] **Step 5:** Commit `feat(auth): email OTP sign-in routes and cookie session identity`.

### Task 4: S3 blob store and presigned uploads

**Files:**
- Create: `backend/app/ingestion/s3_blobs.py`
- Modify: `backend/app/ingestion/blobs.py` (`get_blob_store`), `backend/app/api/uploads.py`, `backend/app/main.py` (mount `local_put_router` only when `blob_backend == "local"`)
- Test: `backend/tests/unit/test_s3_blobs.py`, `backend/tests/unit/test_uploads_presign.py`

**Interfaces:**
- Produces: `class S3BlobStore(BlobStore)` — `__init__(client: Any, bucket: str)`; `put/get/delete` via `asyncio.to_thread` (`put_object` with `ServerSideEncryption="AES256"`, `ContentType="application/pdf"`; `get_object` NoSuchKey → the existing not-found `AppError`); `presign_put(key: str, size_bytes: int, expires_s: int = 300) -> str` using `generate_presigned_url("put_object", Params={"Bucket", "Key", "ContentType": "application/pdf", "ContentLength": size_bytes, "ServerSideEncryption": "AES256"})`. Keys stay under the existing `new_upload_key` scheme, prefixed `uploads/`. `create_upload` returns the presigned URL with headers `{"Content-Type": "application/pdf", "x-amz-server-side-encryption": "AES256"}` when the backend is S3.
- [ ] **Step 1: Failing tests** with botocore `Stubber` and dummy credentials: `test_put_get_delete_roundtrip_calls`; `test_get_missing_maps_to_not_found`; `test_presigned_url_signs_content_length_and_type` (parse query: `X-Amz-SignedHeaders` contains `content-length` and `content-type`); `test_create_upload_s3_returns_presigned_ticket` (key starts with `uploads/<user_id>/`, method PUT); `test_local_put_route_absent_with_s3_backend`.
- [ ] **Step 2–4:** RED → implement → GREEN; lint/typecheck.
- [ ] **Step 5:** Commit `feat(uploads): S3 blob store with presigned PUT uploads`.

### Task 5: Lambda image and 72-hour cleanup

**Files:**
- Create: `backend/app/ops/__init__.py`, `backend/app/ops/cleanup.py`, `backend/app/api/events.py`
- Modify: `backend/Dockerfile`, `backend/app/main.py`
- Test: `backend/tests/integration/test_cleanup.py`, `backend/tests/unit/test_events_route.py`

**Interfaces:**
- Produces:
  - `async def purge_expired(sessionmaker, *, now: datetime, retention: timedelta) -> dict[str, int]` — deletes `documents` with `created_at < now - retention` (cascades), `semantic_cache` with `expires_at < now` or `created_at < now - retention`, `login_codes` with `created_at < now - retention`, `usage_counters` with `window_start < now - retention`; returns counts per table; logs `cleanup_completed` with counts.
  - `POST /events` (LWA pass-through path) mounted **only** when `settings.cleanup_mode` is true; runs `purge_expired` with `retention_hours`; returns counts. Not mounted on the public function.
  - Dockerfile: add `COPY --from=public.ecr.aws/awsguru/aws-lambda-adapter:<latest 0.9.x — check the registry> /lambda-adapter /opt/extensions/lambda-adapter`; env `AWS_LWA_PORT=8000`, `AWS_LWA_READINESS_CHECK_PATH=/health`, `AWS_LWA_INVOKE_MODE=response_stream`, `UPLOAD_DIR=/tmp/uploads`; the image still runs unchanged under docker compose (LWA is inert outside Lambda). Pre-load the spaCy model at import is already done by `warm_up`; keep CMD uvicorn.
- [ ] **Step 1: Failing tests:** `test_purge_removes_only_old_rows` (old and fresh rows in each table; only old ones deleted; users, request_log, guardrail_events untouched); `test_events_route_only_in_cleanup_mode`.
- [ ] **Step 2–4:** RED → implement → GREEN; `docker compose build api && docker compose up -d --wait` still healthy.
- [ ] **Step 5:** Commit `feat(ops): 72-hour cleanup job and Lambda Web Adapter image`.

### Task 6: Secrets helper and AWS CLI deploy/teardown scripts

**Files:**
- Create: `backend/scripts/secrets_set.py`, `infra/aws/config.sh`, `infra/aws/deploy.sh`, `infra/aws/teardown.sh`, `infra/aws/lib.sh`, `infra/aws/policies/` (JSON templates)
- Modify: `Makefile` (`secrets-set`, `migrate-prod`, `deploy`, `teardown`, `smoke-prod`)
- Test: `backend/tests/unit/test_secrets_set.py`, `backend/tests/unit/test_deploy_scripts.py`

Ruling (author's choice): AWS CLI scripts instead of Terraform; state lives in `var/deploy/state.json` (git-ignored).

**Interfaces:**
- `scripts/secrets_set.py --secret-id jobmatch/prod`: reads the current secret JSON if it exists; for each key in the Global Constraints list prompts with `getpass` (Enter keeps the existing value; optional keys may stay empty); generates `SESSION_SIGNING_KEY`/`OTP_HMAC_KEY` with `secrets.token_urlsafe(48)` when absent; validates `DATABASE_URL` starts with `postgresql+psycopg://` and contains `sslmode=require` (offers to rewrite `postgresql://` → `postgresql+psycopg://`); writes with `put-secret-value` (creates the secret, tagged, if missing). Prints key names and "set/kept/generated" only — never a value.
- `infra/aws/config.sh`: `REGION=us-west-2`, `PROJECT=jobmatch-ai`, names (`jobmatch-api`, `jobmatch-cleanup`, ECR repo `jobmatch-api`, bucket `jobmatch-uploads-<account-id>`, roles `jobmatch-lambda`, `jobmatch-scheduler`, secret `jobmatch/prod`, schedule `jobmatch-cleanup-hourly`, budgets `jobmatch-10`, `jobmatch-25`), memory 2048, timeout 300 (cleanup 120), reserved concurrency 5.
- `deploy.sh`: `set -euo pipefail`; requires `BUDGET_EMAIL`; each step is idempotent (describe → create if missing) and **appends the resource to the state file immediately after creating it**; steps: ECR repo (scan on push, lifecycle keep last 3) → `docker build --platform linux/amd64` + push (tag = git short sha) → S3 bucket (block public access, SSE-S3, lifecycle 3 days on `uploads/`, tags) → log groups (14 days) → IAM roles + inline least-privilege policies → api function (image, env: `ENV=prod SECRETS_SOURCE=aws SECRET_ID=jobmatch/prod LLM_PROVIDER=anthropic EMBEDDER_PROVIDER=bedrock RERANKER_PROVIDER=bedrock BLOB_BACKEND=s3 UPLOADS_BUCKET=… EMAIL_SENDER=resend`), reserved concurrency (warn and continue if the account limit refuses) → Function URL (`AuthType NONE`, `InvokeMode RESPONSE_STREAM`) + the public permissions it needs → cleanup function (`CLEANUP_MODE=true`, no URL) → scheduler role + `rate(1 hour)` schedule → two budgets with email alerts at 100% actual → prints the Function URL. Never echoes secret values; `set -x` is forbidden.
- `teardown.sh`: reads the state file and deletes in reverse order, treating "not found" as success; flags `--keep-secret` (default deletes `jobmatch/prod` with `--force-delete-without-recovery` after a typed confirmation unless `--yes`); also sweeps by name from `config.sh` so a lost state file still cleans up; finally lists any remaining resources tagged `Project=jobmatch-ai` via the Resource Groups Tagging API and exits 1 if any remain.
- `make migrate-prod`: `SECRETS_SOURCE=aws SECRET_ID=jobmatch/prod uv run alembic upgrade head`. `make smoke-prod`: `curl` `<url>/health` and expects `"status":"ok"`.
- [ ] **Step 1: Failing tests:** `test_secrets_set_never_prints_values` (fake boto3 client; captured stdout contains key names, not values); `test_secrets_set_generates_missing_keys`; `test_database_url_validation`; `test_deploy_records_state_before_failure` — run `deploy.sh` with a fake `aws`/`docker` on `PATH` (a script that logs argv to a file and fails on a chosen command) → the state file lists every resource created before the failure; `test_teardown_deletes_everything_deploy_created` — run deploy with the fake (all succeed), then teardown → every `create-*`/`put-*` resource has a matching delete in the log; `test_scripts_never_echo_secrets` (no `set -x`; grep the fake-aws log and stdout for a planted secret value).
- [ ] **Step 2–4:** RED → implement → GREEN; `shellcheck infra/aws/*.sh` (via `uvx shellcheck-py` if not installed) clean.
- [ ] **Step 5:** Commit `feat(deploy): secrets helper and idempotent AWS CLI deploy/teardown with state tracking`.

### Task 7: Docs

**Files:**
- Modify: `README.md` (new `## Phase 7: sign-in and deploy` section), `docs/architecture.md` (mark sign-in and deploy as built; note CLI scripts instead of Terraform and why)

Content: sign-in flow and limits; local sign-in with the console sender; prerequisites (Neon project with a `postgresql+psycopg://…?sslmode=require` URL, Resend API key and sender, Anthropic key, `BUDGET_EMAIL`); the exact command order `make secrets-set` → `make migrate-prod` → `make deploy` → `make smoke-prod`; `make teardown` and what it deletes; expected monthly cost; known limits (no frontend yet so no CloudFront; Function URL is the public endpoint; Bedrock guardrail not created).
- [ ] **Step 1:** Write; check every command exists.
- [ ] **Step 2:** `make lint typecheck test`.
- [ ] **Step 3:** Commit `docs: Phase 7 sign-in and deploy`.

### Not a plan task: the live deploy

Run by the controller after the author has created the Neon project, Resend key and Anthropic key and run `make secrets-set` themselves: `make migrate-prod`, `make deploy`, `make smoke-prod`, then a sign-in and sample-flow check. On any failure that cannot be fixed safely, run `make teardown` and confirm nothing tagged `Project=jobmatch-ai` remains.
