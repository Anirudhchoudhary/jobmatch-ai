# Phase 8: Web frontend at jobmatch.ntrye.com (design)

Parent spec: `docs/superpowers/specs/2026-11-26-jobmatch-ai-design.md` (§2 access, §6 architecture,
§9 frontend). Phases 1 to 7 are built; the API is live on a Lambda Function URL in us-west-2.

## 1. Intent and why

Recruiters who find the link on the author's resume must be able to use JobMatch AI in a browser:
sign in with an email code, try the sample (or their own resume and a job description), see the
match analysis, rewrite suggestions and a grounded chat, and see the guardrails at work. The URL
is shared only once this works end to end. The frontend is also portfolio material, so the
engineering (citations, guardrail verdicts, streaming) must be visible, not hidden.

## 2. Goals

- A static Next.js site at `https://jobmatch.ntrye.com` covering the §9 flow (author approved):
  1. **Sign in:** email → 6-digit code → session.
  2. **Documents:** "Try with sample resume + JD" button, or upload a resume PDF and paste a JD.
  3. **Results:** score, matched/partial/missing skills with evidence quotes, suggestions shown
     original vs rewritten.
  4. **Chat:** streamed answers; inline citations (`[r2]`, `[j1]`) are clickable and highlight the
     source chunk text.
  5. **"Show guardrails" panel** per response: which guardrails ran and which fired, with the
     groundedness verdict.
  Plus sign-out, progress steps while long operations stream, and friendly errors (allowance
  reached, demo at capacity, rate limits, expired session).
- The groundedness false positive fixed before sharing (see §6.4).
- **Reload-safe results (author's choice: read endpoints):** small authenticated read endpoints so
  a page reload restores the session's current documents, latest analysis and suggestions
  without re-running (and without spending an allowance); evidence and citations can fetch
  their chunk text so both analysis evidence and chat citations highlight the source.
- ntrye.com DNS moved from GoDaddy to Route 53 (author's choice) without breaking anything that
  uses the domain today.
- Deploy and teardown extended to the new resources with the same guarantees as Phase 7.

## 3. Non-goals

- Server-side rendering, accounts beyond the email code, saved history, mobile apps, i18n.
- Transferring the domain registration away from GoDaddy (only DNS moves).
- Changing the existing site on ntrye.com / www.ntrye.com (it stays on Vercel).

## 4. Anti-goals (failure even if it "works")

- Sign-in email or the existing site on ntrye.com broken by the DNS move, even briefly.
- Teardown deleting the Route 53 hosted zone or any ntrye.com record it did not create.
- A way to reach the API that skips CloudFront and so weakens rate limits or the cookie model.
- Secrets, the user's email or document text in the JavaScript bundle, browser console logs or
  any third-party script (no analytics, no external fonts/CDNs at runtime).
- Anything billed per hour; a surprise bill.

## 5. Constraints

- Budget: AWS ≲ $15/month total. New fixed cost: Route 53 hosted zone $0.50/month (author
  accepted). CloudFront, S3 and ACM stay in free tier at demo traffic.
- Same origin: the session cookie is first-party `SameSite=Lax`, so the site and `/api/*` must be
  served from `jobmatch.ntrye.com`.
- Every mutating `/api` call sends `Content-Type: application/json` (enforced server-side).
- Streaming endpoints are `POST` + Server-Sent Events, so the browser reads them with `fetch` and
  a stream parser (EventSource cannot POST).
- Uploads go browser → S3 via the presigned PUT from `/api/uploads`, sending exactly the headers
  the ticket returns.
- The repo is public: no secret in the frontend, the repo, or the deploy state file.

## 6. Approaches and decisions

### 6.1 Frontend build (author picked A)

| | Approach | Trade-off |
|---|---|---|
| **A (chosen)** | Next.js (App Router) static export on S3, behind CloudFront; `/api/*` forwarded to the Lambda | Matches the parent spec; free tier; one origin; no server pages needed |
| B | Next.js with server rendering on Lambda/Amplify | A second compute service, cold starts and cost for no gain |
| C | Vite + React | Lighter build, but drops the Next.js the author chose; same hosting as A |

Stack (builder may adjust minor libraries): TypeScript, Tailwind, React; no component kit
required; no runtime third-party requests.

### 6.2 Domain and DNS (author: move DNS to Route 53, keep registration at GoDaddy)

- Create a Route 53 public hosted zone for `ntrye.com`. Import **every** existing record from a
  GoDaddy zone-file export the author provides (public lookups can't see all names). Known
  today: apex A → Vercel, `www` CNAME → Vercel, MX → GoDaddy email (secureserver.net), SPF TXT,
  `send.ntrye.com` records and `resend._domainkey` DKIM TXT for Resend, `_dmarc` TXT.
- DNSSEC is not enabled on ntrye.com (no DS/DNSKEY records), so the nameserver switch carries no
  signing risk. The author exports the zone file (BIND) from GoDaddy; it is kept outside the repo.
- Ordering: frontend code proceeds in parallel; certificate, CloudFront and the `jobmatch` alias
  wait until the zone is imported, reviewed, and the nameservers are switched and verified.
- The author reviews a side-by-side list (old vs Route 53) before switching nameservers at
  GoDaddy. After the switch, verify each record resolves identically via public resolvers.
- The hosted zone is **outside** deploy/teardown: created once by a separate, explicit step and
  never deleted by scripts. Deploy only adds `jobmatch.ntrye.com` alias records and ACM
  validation records, and teardown removes only those it recorded.

### 6.3 Hosting and API routing

- ACM certificate for `jobmatch.ntrye.com` in **us-east-1** (CloudFront requirement), DNS-validated
  in Route 53.
- CloudFront distribution, alias `jobmatch.ntrye.com`:
  - default behaviour → private S3 site bucket via Origin Access Control; long cache for hashed
    assets, short for HTML.
  - `/api/*` → the Function URL as a custom HTTPS origin; caching disabled; all viewer headers,
    cookies and query strings forwarded except `Host`; all methods allowed; response streaming
    passes through.
- **Closing the side door (my call):** the Function URL stays `AuthType NONE`, but CloudFront adds
  a secret origin header (`X-Origin-Verify`, value generated at deploy and stored as
  `ORIGIN_VERIFY_TOKEN` in `jobmatch/prod`); in prod the API rejects `/api/*` requests without
  the correct value (403). `/livez` stays open for Lambda readiness. *Rejected alternative:*
  CloudFront OAC with `AuthType AWS_IAM`, which forces the browser to send
  `x-amz-content-sha256` on every POST (more client code, easy to get wrong).
- **Client IP behind CloudFront (my call):** when the origin-verify header is valid, the client IP
  is taken from the `CloudFront-Viewer-Address` header (included via the origin request policy);
  otherwise the request has already been rejected. The Phase 7 `sourceIp` / XFF logic remains the
  fallback for local/ci only.
- S3 uploads bucket gets a CORS rule allowing `PUT` from `https://jobmatch.ntrye.com` with the
  `Content-Type` and `x-amz-server-side-encryption` headers.
- The session cookie keeps `Secure; HttpOnly; SameSite=Lax`; no change needed.

### 6.4 Groundedness false positive (fix before sharing)

Observed live: a correct answer whose first sentence had no citation was flagged, because each
claim is judged only against the chunks it cites. Decision (my call): a claim with no citation is
judged against all context chunks that were given to the model for that answer; claims with
citations are still judged against their cited chunks only. Also tighten the chat prompt to ask
for a citation on every factual sentence. Re-run the offline eval gate and the live CI subset (author
approved the spend, roughly $0.10–0.50); report the measured cost.

### 6.5 Read endpoints (author's choice)

Authenticated `GET` routes, every query filtered by the session's user id and the active
documents (my call on shape): current documents (type, filename, status, chunk count, created
at), the latest analysis for the active pair (or 404), the latest suggestions for it if stored,
and chunk text by id for chunks the user owns and that are not quarantined. No new tables;
suggestions are persisted only if they are not already (builder checks). Results are never put
in browser storage.

### 6.6 "Show guardrails" panel content (my call)

- Chat: the `done` event lists **every** guardrail that ran with its action (allow/redact/flag/
  block), provider and mode, not only those that fired; the panel shows them plus the
  groundedness verdict and any unsupported claims.
- Upload: quarantined chunk count and the guardrail that quarantined them.
- Analysis: downgraded evidence count (hallucination block). Suggestions: rejected rewrite count.

### 6.7 Testing and CI

- Unit/component tests (Vitest + Testing Library): the SSE parser, API client error mapping,
  citation linking, sign-in and results rendering against recorded API responses.
- One browser end-to-end test (Playwright) against the local docker stack with offline
  providers: sign in with the console sender, run the sample, see results and a chat answer.
- CI: a frontend job (lint, typecheck, unit tests, `next build`) on every PR; e2e runs in CI with
  the compose stack.

### 6.8 Deploy and teardown

Extend `infra/aws/deploy.sh` / `teardown.sh` with the same state-file rules: site bucket, OAC,
CloudFront distribution and policies, ACM certificate, Route 53 alias and validation records,
uploads-bucket CORS, origin-verify token. `make deploy-frontend` builds the static export,
syncs it to S3 and invalidates CloudFront. Teardown disables and deletes the distribution
(waiting for it), deletes the certificate, removes only its own Route 53 records, and never
touches the hosted zone.

## 7. Left to the builder

Visual design and copy, component structure, exact cache TTLs, library choices within §6.1,
how progress steps are shown, and page layout, as long as §2's elements are all present and the
anti-goals hold.
