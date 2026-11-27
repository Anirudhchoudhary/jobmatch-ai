# Phase 8: Web Frontend at jobmatch.ntrye.com Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A static Next.js site at `https://jobmatch.ntrye.com` (sign in → documents → results → chat with a guardrails panel), served by CloudFront with `/api/*` forwarded to the existing Lambda, plus the backend changes it needs (groundedness fix, full guardrail list, read endpoints, CloudFront origin verification).

**Architecture:** `frontend/` is a Next.js App Router project exported to static files (`output: "export"`) and served from a private S3 bucket through CloudFront (OAC). CloudFront's `/api/*` behaviour forwards to the Lambda Function URL with a secret `X-Origin-Verify` header; the API rejects `/api/*` without it once a token is configured and takes the client IP from `CloudFront-Viewer-Address`. Streaming endpoints are read with `fetch` + a small SSE parser.

**Tech Stack:** Next.js (App Router, static export), React, TypeScript, Tailwind CSS, Vitest + Testing Library, Playwright; FastAPI backend (Python 3.12); AWS CLI scripts (CloudFront, S3, ACM us-east-1, Route 53).

**Spec:** `docs/superpowers/specs/2026-11-27-phase-8-frontend-design.md` (approved); parent `docs/superpowers/specs/2026-11-26-jobmatch-ai-design.md` §2, §6, §9.

## Global Constraints

- Site: `https://jobmatch.ntrye.com`; Route 53 hosted zone for `ntrye.com` exists (created by hand) and must **never** be created, modified outside the `jobmatch.ntrye.com` names, or deleted by any script.
- Same origin: site and `/api/*` both on `jobmatch.ntrye.com`; session cookie stays `Secure; HttpOnly; SameSite=Lax`.
- Every mutating `/api` call sends `Content-Type: application/json`; streaming endpoints are `POST` + SSE (`event: <name>\ndata: <json>\n\n`), read with `fetch`.
- Uploads: browser PUTs the file to the presigned URL from `POST /api/uploads` with exactly the returned headers.
- No secret, user email or document text in the JS bundle, browser storage, console logs or any third-party request (no analytics, no external fonts/CDNs at runtime).
- Nothing billed per hour. Fixed new cost: Route 53 zone $0.50/month (author accepted). CloudFront/S3/ACM in free tier.
- Teardown removes every resource deploy creates (state file + name sweep), keeps budgets while anything billable remains, and never touches the hosted zone or records it didn't create.
- Groundedness: a claim with no citation is judged against all context chunks given to the model; cited claims only against their cited chunks.
- Read endpoints filter by the session user id and the active documents; nothing is put in browser storage.
- Offline tests stay offline (no real AWS/Anthropic/Resend).

## Review Focus

1. Session expiry mid-use (any `/api` call returns 401 `auth_required`) must send the user back to sign-in with a clear message, not a blank or broken screen — test in Task 5.
2. An SSE stream that ends with an `error` event, or a connection that drops mid-stream, must show a friendly error with the request id and leave the screen usable — test in Task 4.
3. A PDF over 5 MB or not a PDF must be rejected in the browser before any request, with the server's limit in the message — test in Task 6.
4. A citation label in an answer that isn't in `citations` (an invalid citation) must render as plain text, not a broken link — test in Task 8.
5. Allowance (429 `quota_exceeded`/`rate_limited`) and spend-cap (503 `demo_at_capacity`) responses must show their own server message verbatim — test in Task 4.

---

### Task 1: Backend: groundedness fix and full guardrail list in chat

**Files:**
- Modify: `backend/app/guardrails/groundedness.py`, `backend/prompts/chat_system.md` (bump its version), `backend/app/graphs/chat.py`, `backend/app/schemas/chat.py`
- Test: `backend/tests/unit/test_groundedness.py`, `backend/tests/unit/test_chat_graph.py`

**Interfaces:**
- Produces: `ChatDone.guardrail_checks: list[GuardrailOutcome]` — every outcome from input and output stages, including `allow` (the existing `guardrails` field keeps meaning "fired only"). The `GuardContext` for the output stage already carries the sources; a claim whose `citations` is empty is checked against **all** sources in the context.
- [ ] **Step 1: Failing tests:** `test_uncited_claim_checked_against_all_sources` (lexical: answer "Has hands-on Kafka experience. Built a Kafka platform [r2]." with sources r1 "hands-on Kafka and Terraform experience" and r2 "platform on Kafka" → grounded, no unsupported claims); `test_uncited_claim_unsupported_by_any_source_is_flagged`; the same two for `ClaudeGroundedness` with a fake model asserting the prompt for the uncited claim includes every source; `test_chat_done_lists_all_guardrail_checks` (offline graph: `guardrail_checks` contains the allow outcomes of the input guards and the groundedness outcome; `guardrails` still only non-allow).
- [ ] **Step 2–4:** RED → implement → GREEN; `make lint typecheck test`; `make eval` passes the offline gate (record before/after summary in the report).
- [ ] **Step 5:** Commit `fix(guardrails): judge uncited claims against all context; list every guardrail check in chat`.

### Task 2: Backend: read endpoints and stored suggestions

**Files:**
- Create: `backend/app/api/session_state.py`, `backend/migrations/versions/0009_suggestion_sets.py`
- Modify: `backend/app/db/models.py`, `backend/app/graphs/suggest.py` (persist result), `backend/app/analysis/repo.py`, `backend/app/main.py`, `backend/app/ops/cleanup.py` (nothing extra if cascade covers it — check)
- Test: `backend/tests/integration/test_session_state_api.py`

**Interfaces:**
- Produces (all require a session, all `GET`):
  - `GET /api/documents` → `{"documents": [{"doc_id", "doc_type", "source", "filename", "status", "chunk_count", "quarantined_count", "created_at"}]}` — the user's `ready` documents (at most one per type).
  - `GET /api/analyses/latest` → the stored `MatchAnalysis` for the active resume+JD pair (`cached: true`), 404 `analysis_not_found` if none. Does **not** consume an allowance.
  - `GET /api/suggestions/latest` → the stored `BulletSuggestions` for the latest analysis, 404 if none.
  - `GET /api/chunks/{chunk_id}` → `{"chunk_id", "doc_type", "section", "text"}` for a non-quarantined chunk of an active document owned by the user; any other case 404 (never 403, to avoid revealing existence).
  - Table `suggestion_sets(analysis_id uuid pk fk analyses on delete cascade, user_id uuid fk users on delete cascade, result jsonb, created_at timestamptz)`; `suggest()` upserts the result after success.
- [ ] **Step 1: Failing integration tests:** `test_documents_lists_active_docs_only`; `test_latest_analysis_roundtrip_without_allowance` (run analysis, then GET twice, usage counter unchanged); `test_latest_analysis_404_when_none`; `test_latest_suggestions_after_suggest`; `test_chunk_text_owned_only` (another user's chunk id → 404; quarantined chunk → 404); `test_read_endpoints_require_session_in_prod` (401).
- [ ] **Step 2–4:** RED → implement → GREEN; full suites; lint/typecheck.
- [ ] **Step 5:** Commit `feat(api): read endpoints for documents, latest analysis, suggestions and chunk text`.

### Task 3: Backend: CloudFront origin verification and viewer IP

**Files:**
- Create: `backend/app/core/origin_verify.py`
- Modify: `backend/app/core/settings.py`, `backend/app/main.py`, `backend/app/api/auth.py` (`client_ip`)
- Test: `backend/tests/unit/test_origin_verify.py`, `backend/tests/unit/test_client_ip.py` (extend)

**Interfaces:**
- Produces: setting `origin_verify_token: SecretStr | None = None` (env `ORIGIN_VERIFY_TOKEN`, from the secret). When set: pure ASGI middleware returns 403 `{"error":{"code":"forbidden","message":"Forbidden.","request_id":…}}` for any path starting `/api/` whose `x-origin-verify` header doesn't match (constant-time compare); `/livez`, `/health`, `/events` unaffected. When unset: no-op (keeps today's deployment working until CloudFront exists). `client_ip`: when the token is set and verified, use the IP part of `cloudfront-viewer-address` (`"<ip>:<port>"`, IPv6 in brackets or raw — parse both); otherwise existing behaviour.
- [ ] **Step 1: Failing tests:** `test_api_rejected_without_token_when_configured`; `test_api_allowed_with_token`; `test_health_and_livez_open_with_token_configured`; `test_noop_when_token_unset`; `test_wrong_token_is_403_constant_time` (uses `hmac.compare_digest` — assert via monkeypatch spy); `test_client_ip_from_viewer_address_ipv4_and_ipv6`; `test_malformed_viewer_address_falls_back`.
- [ ] **Step 2–4:** RED → implement → GREEN; lint/typecheck.
- [ ] **Step 5:** Commit `feat(api): CloudFront origin verification and viewer-address client IP`.

### Task 4: Frontend scaffold, API client and SSE reader

**Files:**
- Create: `frontend/` (Next.js App Router, TypeScript, Tailwind, ESLint, Vitest), `frontend/next.config.ts` (`output: "export"`, `images.unoptimized`, dev-only rewrite of `/api/:path*` → `http://localhost:8000/api/:path*`), `frontend/src/lib/api.ts`, `frontend/src/lib/sse.ts`, `frontend/src/lib/types.ts`
- Modify: `Makefile` (`fe-install`, `fe-dev`, `fe-test`, `fe-build`), `.gitignore` (`frontend/node_modules`, `frontend/out`, `frontend/.next`)
- Test: `frontend/src/lib/api.test.ts`, `frontend/src/lib/sse.test.ts`

**Interfaces:**
- Produces:
  - `types.ts`: TS types mirroring `MatchAnalysis`, `SkillMatch`, `Evidence`, `BulletSuggestions`, `ChatDone` (incl. `guardrail_checks`), `Citation`, `Verdict`, `GuardrailOutcome`, `DocumentReady`, `UploadTicket`, `ApiErrorBody {error:{code,message,request_id}}`.
  - `api.ts`: `class ApiError extends Error {status; code; requestId}`; `apiGet<T>(path): Promise<T>`; `apiPost<T>(path, body?: unknown): Promise<T>` (always JSON content type, `credentials: "same-origin"`, body `{}` when none); non-2xx → `ApiError` with the server's `message` verbatim (fallback "Something went wrong. Please try again."); 401 `auth_required` additionally calls a registered `onUnauthorized()` handler.
  - `sse.ts`: `streamSse(path, body, onEvent: (name: string, data: unknown) => void, signal?): Promise<void>` — POSTs JSON, parses chunked `event:`/`data:` blocks across arbitrary chunk boundaries, routes an `error` event to a thrown `ApiError` (status 0, code from payload), throws `ApiError` on non-2xx before streaming, and throws `ApiError("stream_interrupted", "The connection dropped. Please try again.")` if the body ends without a `done` or `error` event.
- [ ] **Step 1: Failing tests:** `sse.test.ts`: events split across chunks mid-line; multiple events in one chunk; `error` event → rejects with its code/message; stream ending without `done` → `stream_interrupted`; non-2xx JSON error → `ApiError` with message. `api.test.ts`: JSON content type and `{}` body on POST; 429 message verbatim; 503 `demo_at_capacity` message verbatim; 401 triggers `onUnauthorized`.
- [ ] **Step 2–4:** RED → implement → GREEN; `npm run lint && npm run typecheck && npm test && npm run build` (static export produces `frontend/out/index.html`).
- [ ] **Step 5:** Commit `feat(frontend): Next.js static scaffold, API client and SSE reader`.

### Task 5: Frontend: sign-in and session

**Files:**
- Create: `frontend/src/app/page.tsx` (landing + sign-in), `frontend/src/components/SignIn.tsx`, `frontend/src/lib/session.tsx` (context: `user | null`, `refresh()`, `signOut()`), `frontend/src/app/app/page.tsx` (signed-in workspace shell with sign-out)
- Test: `frontend/src/components/SignIn.test.tsx`, `frontend/src/lib/session.test.tsx`

**Interfaces:**
- Consumes: Task 4 `apiGet`, `apiPost`, `ApiError`, `onUnauthorized`.
- Produces: `useSession(): {user: {user_id: string; email: string} | null; loading: boolean; refresh(): Promise<void>; signOut(): Promise<void>}`; routes `/` (landing, sign-in) and `/app/` (workspace; redirects to `/` when signed out). Sign-in: email step (`POST /api/auth/request-code`) → code step (`POST /api/auth/verify`, 6-digit numeric input, resend link) → `/app/`.
- [ ] **Step 1: Failing tests:** `SignIn.test.tsx`: submitting an email shows the code step regardless of whether the email is new; invalid code shows the server message; 429 shows the server message; successful verify navigates to `/app/`. `session.test.tsx`: any 401 `auth_required` clears the user and routes to `/` with "Your session expired. Sign in again."; `signOut` posts logout and clears the user.
- [ ] **Step 2–4:** RED → implement → GREEN; lint/typecheck/build.
- [ ] **Step 5:** Commit `feat(frontend): email code sign-in and session handling`.

### Task 6: Frontend: documents (sample, upload, paste JD)

**Files:**
- Create: `frontend/src/components/Documents.tsx`, `frontend/src/components/Progress.tsx`, `frontend/src/lib/upload.ts`
- Test: `frontend/src/components/Documents.test.tsx`, `frontend/src/lib/upload.test.ts`

**Interfaces:**
- Consumes: Task 4 `apiGet`/`apiPost`/`streamSse`, Task 2 `GET /api/documents`.
- Produces: `uploadResume(file: File): Promise<string /* upload key */>` (checks type `application/pdf` and size ≤ 5 MB **before** any request; `POST /api/uploads` → PUT to `ticket.url` with exactly `ticket.headers`); `Documents` component: "Try with sample resume + JD" (`POST /api/samples` SSE), resume upload then `POST /api/stream/documents {doc_type:"resume", upload_key, filename}`, JD textarea (50–20000 chars) → `POST /api/stream/documents {doc_type:"jd", text}`; shows progress steps from `progress` events and a quarantine notice from `done.quarantined_count`/`guardrails`; calls `onReady()` when both documents are ready (checked via `GET /api/documents`).
- [ ] **Step 1: Failing tests:** non-PDF and 6 MB file rejected with no fetch call and messages "Upload a PDF file." / "That file is too large. The limit is 5 MB."; presigned PUT sends exactly the ticket headers; sample button streams progress and calls `onReady`; JD under 50 chars disables submit; quarantine notice shown when `quarantined_count > 0`.
- [ ] **Step 2–4:** RED → implement → GREEN; lint/typecheck/build.
- [ ] **Step 5:** Commit `feat(frontend): sample, resume upload and job description input`.

### Task 7: Frontend: results (analysis, evidence, suggestions, reload restore)

**Files:**
- Create: `frontend/src/components/Analysis.tsx`, `frontend/src/components/Suggestions.tsx`, `frontend/src/components/ChunkViewer.tsx`
- Test: `frontend/src/components/Analysis.test.tsx`, `frontend/src/components/Suggestions.test.tsx`

**Interfaces:**
- Consumes: Task 2 read endpoints; `POST /api/stream/analyses` (SSE), `POST /api/suggestions`.
- Produces: on mount, `GET /api/analyses/latest` and `GET /api/suggestions/latest` restore results (404 → show "Run analysis" button). Analysis view: score, required / nice-to-have coverage, skills grouped matched/partial/missing with importance, each evidence quote clickable → `ChunkViewer` fetches `GET /api/chunks/{id}` and shows the chunk with the quote highlighted; "downgraded evidence" count shown in the guardrails panel slot. Suggestions view: original vs rewritten side by side, rationale, target skills, rejected count.
- [ ] **Step 1: Failing tests:** restores from `/api/analyses/latest` without calling the POST; 404 shows the run button; skills grouped by status with counts; clicking evidence fetches the chunk and highlights the quote (case-insensitive substring); suggestions render original/rewritten pairs and the rejected count.
- [ ] **Step 2–4:** RED → implement → GREEN; lint/typecheck/build.
- [ ] **Step 5:** Commit `feat(frontend): analysis, evidence viewer and suggestions with reload restore`.

### Task 8: Frontend: chat with citations and guardrails panel

**Files:**
- Create: `frontend/src/components/Chat.tsx`, `frontend/src/components/AnswerText.tsx`, `frontend/src/components/GuardrailsPanel.tsx`
- Test: `frontend/src/components/Chat.test.tsx`, `frontend/src/components/AnswerText.test.tsx`, `frontend/src/components/GuardrailsPanel.test.tsx`

**Interfaces:**
- Consumes: Task 4 `streamSse`, Task 1 `ChatDone.guardrail_checks`, `Verdict`.
- Produces: chat input (max length per `ChatRequest`), streamed `token` text, then `verdict` and `done`; `AnswerText` turns `[r1]`/`[j2]` labels present in `done.citations` into buttons that open the cited chunk text (from `Citation.text`); labels not in `citations` stay plain text; a flagged verdict (`action: "flag"`) shows a clear "parts of this answer could not be verified" banner listing `unsupported_claims`; blocked/refused answers styled distinctly. `GuardrailsPanel` (toggle "Show guardrails") lists every check (guardrail, provider, stage, action, mode) with fired ones highlighted.
- [ ] **Step 1: Failing tests:** tokens render incrementally; valid label → button that reveals the chunk; invalid label → plain text; flag verdict banner lists claims; panel lists allow and block outcomes and highlights non-allow; send disabled while streaming; `stream_interrupted` shows the error and re-enables input.
- [ ] **Step 2–4:** RED → implement → GREEN; lint/typecheck/build.
- [ ] **Step 5:** Commit `feat(frontend): streaming chat, clickable citations and guardrails panel`.

### Task 9: End-to-end test and CI

**Files:**
- Create: `frontend/e2e/flow.spec.ts`, `frontend/playwright.config.ts`
- Modify: `.github/workflows/ci.yml` (new `frontend` job: `npm ci`, lint, typecheck, unit tests, build; and an `e2e` job: compose Postgres + API with offline providers, `next dev`, Playwright chromium), `Makefile` (`fe-e2e`)

**Interfaces:**
- Consumes: everything above. The sign-in code comes from the local console sender's `login_code_console` log line, read in the test with `docker compose logs api` (no test-only API hook, so nothing extra ships to production). Also add `trailingSlash: true` to `next.config.ts` if Task 4 didn't.
- [ ] **Step 1:** Write `flow.spec.ts`: sign in (code from the local console sender's log line `login_code_console`), try the sample, run analysis, see a score, ask a chat question, open the guardrails panel and see at least one check, reload and see the analysis restored, sign out.
- [ ] **Step 2:** Run locally against `docker compose` (offline providers) → PASS.
- [ ] **Step 3:** Add the CI jobs; confirm green on the PR.
- [ ] **Step 4:** Commit `ci(frontend): unit tests, static build and Playwright end-to-end`.

### Task 10: Infra: CloudFront, certificate, DNS records, site deploy, teardown

**Files:**
- Modify: `infra/aws/config.sh`, `infra/aws/deploy.sh`, `infra/aws/teardown.sh`, `infra/aws/lib.sh`, `infra/aws/policies/` (site bucket policy for OAC, uploads CORS), `Makefile` (`deploy-frontend`), `backend/tests/unit/fake_aws.py`, `backend/tests/unit/test_deploy_scripts.py`

**Interfaces:**
- Consumes: Task 3 `ORIGIN_VERIFY_TOKEN`; Route 53 zone found by name `ntrye.com.` (never created here); `DOMAIN=jobmatch.ntrye.com`.
- Produces (deploy, each recorded in the state file right after creation): origin-verify token added to `jobmatch/prod` if absent (`secrets.token_urlsafe(48)`, merged into the existing JSON without printing anything); uploads bucket CORS (`PUT` from `https://jobmatch.ntrye.com`, headers `Content-Type`, `x-amz-server-side-encryption`); private site bucket `jobmatch-site-<account>` (block public access, SSE-S3); CloudFront OAC; ACM certificate in **us-east-1** with DNS validation records UPSERTed into the zone (recorded) and `aws acm wait certificate-validated`; CloudFront distribution (alias `jobmatch.ntrye.com`, default → site bucket via OAC with managed CachingOptimized, `/api/*` → Function URL host with managed CachingDisabled + AllViewerExceptHostHeader + an origin request policy that includes `CloudFront-Viewer-Address`, custom origin header `X-Origin-Verify` from the secret, `PriceClass_100`, HTTP/2+3, redirect HTTP→HTTPS, default root object `index.html`; Next export uses `trailingSlash: true` so `/app/` maps to `app/index.html` and no error-page rewrites are needed); site bucket policy allowing only that distribution; Route 53 A/AAAA alias `jobmatch.ntrye.com` → distribution (recorded). `make deploy-frontend`: `npm ci && npm run build` then `aws s3 sync frontend/out s3://<site bucket> --delete` (HTML `cache-control: no-cache`, hashed assets long cache) and `aws cloudfront create-invalidation --paths '/*'`. Teardown: remove alias and validation records it recorded (exact values via `DELETE` changes), disable distribution → wait deployed → delete, delete OAC, delete certificate (us-east-1), empty + delete site bucket, remove uploads CORS; never call `delete-hosted-zone`; the name sweep must not delete any record other than `jobmatch.ntrye.com` A/AAAA and the recorded `_…jobmatch.ntrye.com` validation CNAME.
- [ ] **Step 1: Failing tests** (fake aws): deploy records every new resource; teardown deletes all of them in safe order; teardown never calls `delete-hosted-zone` and only deletes record sets named `jobmatch.ntrye.com.` or the recorded validation name; token merge never prints the secret and preserves existing keys; distribution config has the origin header, viewer-address policy and caching-disabled `/api/*`.
- [ ] **Step 2–4:** RED → implement → GREEN; shellcheck; lint/typecheck.
- [ ] **Step 5:** Commit `feat(deploy): CloudFront, ACM, Route 53 alias and static site deploy/teardown`.

### Task 11: Docs

**Files:**
- Modify: `README.md` (Phase 8 section: run the frontend locally, `make deploy-frontend`, the site URL, the CloudFront origin check, known limits), `docs/architecture.md` (frontend + CloudFront now built; update diagram and Part 4)
- [ ] **Step 1:** Write; verify every command exists.
- [ ] **Step 2:** `make lint typecheck test` and `npm run lint` in `frontend/`.
- [ ] **Step 3:** Commit `docs: Phase 8 frontend and CloudFront`.

### Not plan tasks (controller, after merge)

1. After the author switches nameservers: verify every imported record through public resolvers.
2. `make deploy` (adds token, CloudFront, certificate, DNS), `make deploy-frontend`, then live checks at `https://jobmatch.ntrye.com`: sign in, sample, analysis, chat with citations and guardrails panel, reload restore, direct Function URL `/api/*` → 403, upload from the browser.
3. Live eval CI subset (author approved): `make eval-live-baseline` and `make eval-live`; report measured cost.
