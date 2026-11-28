# Frontend redesign: guided, self-explanatory UI (design)

Parent designs: `docs/superpowers/specs/2026-11-27-phase-8-frontend-design.md` (current frontend),
`docs/superpowers/specs/2026-11-26-jobmatch-ai-design.md` §9. Site live at https://jobmatch.ntrye.com.

## 1. Intent and why

The site will be shared with recruiters from the author's resume. Today it works but looks dated
("like a 2000s website"), stacks every feature in one long column, and shows raw citation codes
("j1", "r1r3") inside answers. The author wants it to feel like a polished modern AI product that
explains itself, in the way Anthropic's own console does: you can always see that something
happened (a file is uploaded, the model is thinking and on which step) and answers appear
progressively, so the user senses something being generated.

## 2. Goals (agreed with the author)

1. **Visual style "Calm light" (chosen from mockups, option A):** white and soft greys, a single
   indigo→teal accent (`#4f46e5` → `#06b6d4`), rounded cards, generous spacing, Linear/Notion feel.
2. **Layout "Guided steps" (chosen, option 1):** one step on screen at a time under a progress bar
   `1 Documents → 2 Match → 3 Improve → 4 Ask`. Finished steps show a tick and are clickable to go
   back. Finishing a step advances automatically.
3. **Landing page:** headline "See how well your resume fits a job, with the evidence.", a one-line
   value statement (match score · resume rewrites · a chat that cites your documents · guardrails
   you can inspect), and the sign-in card (email → code), restyled.
4. **Step 1 · Documents:** prominent "Try the sample" banner; a drag-and-drop resume PDF zone; a
   job-description text box. Each document shows a live status card (`✓ resume.pdf uploaded ·
   reading → scanning for injection → indexing → done`, quarantine notice if any). When both
   documents are ready the flow moves to Step 2 on its own.
5. **Step 2 · Match:** the analysis **starts automatically** when the step opens and no stored
   analysis exists for the current documents; its pipeline steps are shown live ("✓ Reading the
   job · ✓ Reading the resume · ◌ Matching skills with evidence…"). Result: a score ring, required
   / nice-to-have coverage, skills colour-coded matched / partial / missing with importance, each
   with its evidence quote and "view in resume" (existing chunk viewer). "Next: improve my resume →".
6. **Step 3 · Improve:** suggestions **run automatically** when the step opens if none are stored;
   before / after side by side with rationale and target skills; a note when rewrites were blocked
   for adding facts not in the resume. "Next: ask questions →".
7. **Step 4 · Ask:** clickable starter questions; each answer shows its working steps
   ("✓ Checked your question · ✓ Searched N passages · ✓ Verified against sources") as they
   happen, then the text **types out smoothly**, then numbered **sources**.
8. **Citations:** `[r2]` / `[j1]` labels render as superscript numbers (¹ ²) numbered in order of
   first appearance in that answer; below the answer a "Sources" row lists `1 Resume · Summary`,
   `2 Job description · Requirements` (doc type + section); clicking a number or a source shows
   the quoted chunk text. Copying the answer text never produces "r1r3". Unknown labels (invalid
   citations) are dropped from the rendered text (they carry no source).
9. **Smooth typing:** streamed tokens are revealed at a steady, readable pace (roughly 1–3
   characters per animation frame, faster when a backlog builds so the text never lags the stream
   by more than ~1 second); a blinking cursor shows while writing; reduced-motion users get the
   text immediately.
10. **Reload restore:** after a reload the user lands on the furthest step their data supports
    (documents ready → Match; analysis stored → Improve available; etc.) with results intact
    (existing read endpoints).
11. **Icon:** replace the Next.js favicon with a custom JobMatch AI icon — a white resume page and a
    match tick on a rounded indigo→teal gradient square (as mocked) — as `src/app/icon.svg`, plus
    `apple-icon.png` (180×180) generated from it; remove `favicon.ico`. Use the same mark as the logo
    in the header.

## 3. Non-goals

- No backend or API changes; no new endpoints; no infra changes beyond `make deploy-frontend`.
- No dark mode redesign (keep the current dark tokens working but not polished), no i18n, no
  multi-turn chat memory, no new features beyond what's listed.

## 4. Anti-goals

- Breaking anything that works today: sign-in, session expiry handling, upload validation,
  presigned uploads, SSE error handling, reload restore, guardrails panel, no browser storage of
  user data, no raw HTML rendering of model output.
- Third-party runtime requests (no web fonts, CDNs or analytics).
- Hiding guardrail behaviour: blocked / refused / flagged answers stay unmistakable.
- Spending allowances without the user's intent beyond the two agreed auto-runs (Step 2 analysis,
  Step 3 suggestions) — never re-run on reload when stored results exist, never loop.

## 5. Constraints

- Existing stack: Next.js static export, React, Tailwind, Vitest + Testing Library, Playwright.
- Accessibility: keyboard reachable steps and citations, visible focus, `aria-current="step"`,
  live regions for status updates, `prefers-reduced-motion` respected, colour contrast AA.
- Works from 360 px mobile width to desktop.

## 6. Approaches (author chose A)

| | Approach | Trade-off |
|---|---|---|
| **A (chosen)** | Restyle and re-arrange: keep existing components' logic and tests; new tokens, rebuilt UI primitives, a guided-steps shell, citation/typing changes, icon; CSS animations; `lucide-react` icons (bundled) | Fastest, safest; looks fully new |
| B | Same plus shadcn/ui (Radix) | More dependencies and rewrites for little visible gain |
| C | Rebuild from scratch | Throws away tested behaviour; more risk before sharing |

## 7. Decided details (my calls, author can correct)

- Auto-runs: Step 2 analysis and Step 3 suggestions start automatically only when the step opens
  and no stored result exists for the current documents (1 of 5 analyses / 1 of 10 suggestions
  per day); explicit "Re-run" buttons stay.
- Step advance after documents: automatic once both documents are ready, with a short "Both
  documents ready" confirmation (≈1 s) so the transition is noticeable.
- Working-steps line in chat uses the existing SSE `progress` events (checking_input, retrieving,
  reranking, generating) mapped to plain words; "Verified" appears when the `verdict` arrives.
- Sources row shows doc type and section from `done.citations`; numbering resets per answer.
- **Ingestion starts immediately (author, after approval):** dropping or choosing a resume PDF
  starts the upload to S3 and indexing into pgvector at once (no separate upload button), with
  the live status card. Pasting a job description of at least 50 characters starts indexing at
  once; text typed by hand gets an "Add job description" button so partial text is never indexed.
  When both are ready, Step 1 advances to Match and the analysis runs (as below).
- **When paid runs start (refines §2.5/§2.6):** a paid run starts automatically only when the
  user's own action brought them to the step — the automatic move from Step 1 after uploading,
  or clicking a "Next" button / a step in the progress bar. A page reload never starts one: it
  lands on the furthest step the stored data supports and, if that step has no result yet, shows
  a "Run analysis" / "Suggest rewrites" button instead.
- **Advancing:** only Step 1 advances by itself (to Match). Steps 2 and 3 move on with their
  "Next" buttons, so the user sees the score before suggestions are spent.
- **Which steps are open:** Documents is always open; Match opens when both documents are ready;
  Improve and Ask open once an analysis exists (Ask does not require Step 3). Locked steps in the
  progress bar are visibly disabled with a short hint.
- **Replacing a document:** when the user comes back to Step 1, finishing an upload does not move
  them on; a "Continue to your match →" button appears instead. A replaced document clears the
  Match and Improve ticks and hides their old results (the read endpoints already return nothing
  for a new document pair), so nothing stale is shown as current.
- **Chat working steps without invented numbers:** the line reads "✓ Checked your question ·
  ✓ Searched your documents · ✓ Verified against sources"; once the answer is done it adds
  "· N sources cited" from `done.citations`. No retrieval count is shown, because the API does
  not send one.

## 8. Left to the builder

Exact spacing, shadows, type scale, animation timings within §2.9, illustration-free empty
states, component file structure, and copy wording beyond the strings quoted above.
