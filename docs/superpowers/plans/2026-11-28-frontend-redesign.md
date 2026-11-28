# Frontend Redesign (Calm light, Guided Steps) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restyle and re-arrange the existing Next.js frontend into a modern "Calm light" guided four-step flow with self-explanatory status, smooth typing, numbered citations and a custom icon, without changing the backend.

**Architecture:** Keep the existing components' data logic and tests (api/sse/session/upload libs, Documents, Analysis, Suggestions, Chat, ChunkViewer, GuardrailsPanel) and change their presentation; add a small flow state machine (`useFlow`) and a `Stepper` shell that shows one step at a time; add a `useTypewriter` hook and a citation renderer. New design tokens in `globals.css`, rebuilt `ui/` primitives, `lucide-react` icons, CSS-only animations.

**Tech Stack:** Next.js (static export), React, TypeScript, Tailwind v4, Vitest + Testing Library, Playwright, lucide-react.

**Spec:** `docs/superpowers/specs/2026-11-28-frontend-redesign-design.md` (approved; §7 holds the author's later decisions).

## Global Constraints

- Calm light: white / soft greys, single accent gradient `#4f46e5` → `#06b6d4`, rounded cards, Linear/Notion feel. Dark-mode tokens keep working but are not redesigned.
- Steps: `1 Documents → 2 Match → 3 Improve → 4 Ask`; finished steps ticked and clickable; locked steps visibly disabled with a hint. Documents always open; Match opens when both documents are ready; Improve and Ask open once an analysis exists.
- Ingestion starts on resume drop/choose; a JD paste of ≥50 characters ingests at once; typed JD gets an "Add job description" button.
- Only Step 1 advances by itself (to Match), and only when not revisiting; revisiting Step 1 shows "Continue to your match →". A replaced document clears Match/Improve ticks and hides their results.
- Paid auto-runs (analysis in Match, suggestions in Improve) start only when the user's own action brought them to the step (Step 1 auto-advance, Next button, progress-bar click); never on page reload/restore (show "Run analysis" / "Suggest rewrites" instead); never twice for the same documents.
- Citations: `[r2]`/`[j1]` → superscript numbers in order of first appearance per answer; a Sources row `N Resume · <section>` / `N Job description · <section>`; clicking reveals the chunk quote; invalid labels dropped; copying never yields "r1r3".
- Typing: steady reveal ~1–3 chars per frame, never lagging the stream by more than ~1 s; blinking cursor while writing; `prefers-reduced-motion` shows text immediately.
- Chat working line: "✓ Checked your question · ✓ Searched your documents · ✓ Verified against sources", plus "· N sources cited" after done; no invented counts.
- Icon: `src/app/icon.svg` (white resume page + match tick on rounded indigo→teal square, as mocked) and `src/app/apple-icon.png` 180×180; remove `src/app/favicon.ico`; same mark as header logo.
- Keep: no browser storage of user data, no raw HTML for model output, no third-party runtime requests (no web fonts/CDNs), every existing behaviour test still passes (update assertions only where text/markup intentionally changed).
- Accessibility: keyboard reachable steps and citations, `aria-current="step"`, live regions for status, visible focus, AA contrast, 360 px mobile width.

## Review Focus

1. Reload with stored analysis but no suggestions must not spend a suggestion: Improve shows "Suggest rewrites" — test in Task 3.
2. Replacing only one document while revisiting Step 1 must not start a paid analysis on a half-old pair — test in Task 3.
3. A stream error mid-answer must stop the typewriter cleanly, show the error, and keep already-revealed text — test in Task 2.
4. A JD typed (not pasted) must never auto-ingest; a paste shorter than 50 characters must not ingest — test in Task 4.
5. An answer whose citations reference unknown labels renders no stray numbers or codes — test in Task 2.

---

### Task 1: Design system, icon and logo

**Files:** Modify `frontend/src/app/globals.css`, `frontend/src/components/ui/{Button,Card,Input,Alert}.tsx`, `frontend/src/app/layout.tsx`; Create `frontend/src/components/ui/{Badge,Logo,StatusLine}.tsx`, `frontend/src/app/icon.svg`, `frontend/src/app/apple-icon.png`; Delete `frontend/src/app/favicon.ico`; Test `frontend/src/components/ui/ui.test.tsx`.

**Interfaces — Produces:** tokens `--brand` (#4f46e5), `--brand-2` (#06b6d4), `--success`, `--warning`, `--danger` with soft variants, `--radius-card`; `Button` variants `primary | secondary | ghost` + `loading` prop (spinner, disabled); `Card` with optional `title`/`actions`; `Badge` tones `neutral | brand | success | warning | danger`; `Logo` (`size?: number`, renders the icon mark + "JobMatch AI"); `StatusLine` (`items: {label: string; state: "done" | "active" | "pending"}[]`, renders ✓ / spinner / ○ with an `aria-live="polite"` region). `lucide-react` added (pinned).
- [ ] Step 1: failing tests — Button loading is disabled and shows a spinner with an accessible label; Badge renders each tone; StatusLine marks the active item and announces via a polite live region; Logo renders an `svg` with `aria-hidden` and the text "JobMatch AI".
- [ ] Step 2–4: RED → implement → GREEN; `npm run lint && npm run typecheck && npm test && npm run build`; confirm `out/icon.svg` exists and no `favicon.ico` in `out/`; generate `apple-icon.png` from the SVG with a local tool (e.g. `npx @resvg/resvg-js-cli` or sharp in a one-off script, not a runtime dep) and commit the PNG.
- [ ] Step 5: commit `feat(frontend): Calm light design system, UI primitives and JobMatch icon`.

### Task 2: Numbered citations, smooth typing and chat working line

**Files:** Modify `frontend/src/components/AnswerText.tsx`, `frontend/src/components/Chat.tsx`; Create `frontend/src/lib/typewriter.ts`, `frontend/src/lib/citations.ts`; Test `frontend/src/lib/typewriter.test.ts`, `frontend/src/lib/citations.test.ts`, update `AnswerText.test.tsx`, `Chat.test.tsx`.

**Interfaces — Produces:** `numberCitations(text: string, citations: Citation[]): {parts: Array<string | {n: number; label: string}>; sources: Array<{n: number; citation: Citation}>}` (unknown labels removed, numbering by first appearance, repeated labels reuse their number); `useTypewriter(target: string, opts: {done: boolean; reducedMotion?: boolean}): {shown: string; typing: boolean}` (rAF-driven, adaptive speed so the backlog clears within ~1 s, immediate when reduced motion or when `done` and backlog is tiny). `AnswerText` renders superscript buttons (`aria-label="Source N: Resume · Summary"`) and a Sources row of chips; clicking a number or chip toggles the quote panel (existing `Citation.text`). `Chat` shows the working line from `progress` events (checking_input/retrieving/reranking/generating → the three labels), "· N sources cited" after `done`, the typewriter for `token` text, and a blinking cursor while typing.
- [ ] Step 1: failing tests — `numberCitations` (first-appearance order; repeat reuse; unknown dropped; adjacent `[r1][r3]` → ¹ ³ with no letters); `useTypewriter` with fake timers/rAF reveals gradually, catches up within ~1 s for a 2,000-char backlog, shows all at once under reduced motion; `AnswerText` text content contains no `r1`/`j2` codes and the Sources row lists "1 Resume · Summary"; `Chat`: working line progresses through the three labels and ends with "2 sources cited"; a stream error keeps revealed text, stops the cursor and shows the error (Review Focus 3); copying (`textContent` of the answer paragraph) has no label codes.
- [ ] Step 2–4: RED → implement → GREEN; lint/typecheck/test/build.
- [ ] Step 5: commit `feat(frontend): numbered sources, smooth typing and chat working steps`.

### Task 3: Guided flow state machine, step shell and landing

**Files:** Create `frontend/src/lib/flow.ts`, `frontend/src/components/Stepper.tsx`, `frontend/src/components/Workspace.tsx`; Modify `frontend/src/app/app/page.tsx`, `frontend/src/app/page.tsx`, `frontend/src/components/SignIn.tsx` (restyle only); Test `frontend/src/lib/flow.test.ts`, `frontend/src/components/Workspace.test.tsx`, `frontend/src/components/Stepper.test.tsx`.

**Interfaces — Produces:** `type StepId = "documents" | "match" | "improve" | "ask"`; `useFlow(): {step: StepId; arrival: "user" | "restore"; unlocked: Record<StepId, boolean>; done: Record<StepId, boolean>; goTo(step: StepId): void; onDocumentsReady(changed: boolean): void; onAnalysis(present: boolean): void; onSuggestions(present: boolean): void; revisitingDocuments: boolean}` — restore on mount from `GET /api/documents`, `/api/analyses/latest`, `/api/suggestions/latest` (furthest supported step, `arrival: "restore"`); `goTo` sets `arrival: "user"`; Step 1 auto-advances only on first completion (not when revisiting), else exposes `revisitingDocuments` for the "Continue to your match →" button; a changed document clears match/improve done flags. `Stepper` renders the progress bar (`aria-current="step"`, ticks, disabled locked steps with hint text). `Workspace` renders one step at a time and passes `autoRun = arrival === "user" && !hasResult` to Analysis/Suggestions. Landing: headline "See how well your resume fits a job, with the evidence.", value line, restyled sign-in card, logo header.
- [ ] Step 1: failing tests — restore lands on furthest step with `arrival: "restore"`; reload with analysis but no suggestions → Improve unlocked, Suggestions mounted with `autoRun=false` (Review Focus 1); first documents-ready → step `match`, `arrival: "user"`; revisiting Step 1 and replacing one doc → stays on documents, shows Continue, match/improve ticks cleared, no analysis POST (Review Focus 2); locked step click does nothing and shows the hint; Ask unlocked once analysis exists without Step 3.
- [ ] Step 2–4: RED → implement → GREEN; lint/typecheck/test/build.
- [ ] Step 5: commit `feat(frontend): guided four-step flow with restore and landing redesign`.

### Task 4: Step 1 — documents: ingest on drop/paste, status cards

**Files:** Modify `frontend/src/components/Documents.tsx`, `frontend/src/components/Progress.tsx`; Test update `frontend/src/components/Documents.test.tsx`.

**Interfaces — Consumes:** Task 1 primitives/StatusLine, Task 3 `onDocumentsReady(changed)`. **Produces:** "Try the sample" banner; drag-and-drop + click resume zone that starts upload+ingest immediately on drop/choose (existing type/size checks first); JD textarea that ingests immediately on a paste producing ≥50 chars, otherwise shows "Add job description"; per-document status card (file name / "Job description", live steps uploading → reading → scanning → indexing → done, quarantine notice, replace action).
- [ ] Step 1: failing tests — dropping a PDF triggers the upload POST and the stream without any button click; non-PDF / >5 MB still rejected before any request; pasting 60 chars ingests once; typing 60 chars does not ingest until "Add job description" is clicked; pasting 30 chars does not ingest (Review Focus 4); status card shows the live step labels and "done".
- [ ] Step 2–4: RED → implement → GREEN; lint/typecheck/test/build.
- [ ] Step 5: commit `feat(frontend): instant ingest on drop/paste with live status cards`.

### Task 5: Steps 2–3 — match and improve screens

**Files:** Modify `frontend/src/components/Analysis.tsx`, `frontend/src/components/Suggestions.tsx`, `frontend/src/components/ChunkViewer.tsx`; Create `frontend/src/components/ScoreRing.tsx`; Test update `Analysis.test.tsx`, `Suggestions.test.tsx`.

**Interfaces — Consumes:** `autoRun: boolean` from Workspace, Task 1 primitives/StatusLine. **Produces:** Analysis: when `autoRun` and no stored result → starts the stream on mount once; live StatusLine of analysis progress steps (friendly labels); ScoreRing (score/100, `role="meter"`), required / nice-to-have coverage, skills grouped matched/partial/missing with coloured badges and importance, evidence quote + "view in resume"; "Next: improve my resume →" calls `goTo("improve")`; `onAnalysis(true)` after success. Suggestions: when `autoRun` and none stored → starts once; before/after side by side, rationale, target-skill badges, blocked-rewrites note; "Next: ask questions →".
- [ ] Step 1: failing tests — `autoRun=true` + 404 latest → exactly one POST; `autoRun=false` + 404 → button shown, no POST; stored result → no POST in either case; ScoreRing exposes value/min/max; Next buttons call `goTo` with the right step.
- [ ] Step 2–4: RED → implement → GREEN; lint/typecheck/test/build.
- [ ] Step 5: commit `feat(frontend): match and improve screens with score ring and auto-run rules`.

### Task 6: Step 4 — ask screen polish, e2e and docs

**Files:** Modify `frontend/src/components/Chat.tsx` (layout, starter questions), `frontend/src/components/GuardrailsPanel.tsx` (restyle), `frontend/e2e/flow.spec.ts`, `README.md` (Phase 8 screenshots/copy section only); Test update `Chat.test.tsx`.

**Interfaces — Produces:** starter question chips ("What's my biggest gap?", "Do I have the required experience?", "Which skills should I highlight?") that send immediately; chat history kept in component state while switching steps (lift to Workspace if needed); answer cards with blocked/refused/flagged styling intact; e2e updated to the new flow (sign in → sample → auto-advance to Match → score visible → Improve → Ask → answer with a Sources row → reload restores Match without new POST → sign out).
- [ ] Step 1: failing tests — clicking a starter chip sends that question; history survives a step switch; e2e updated and passing locally (`make fe-e2e`).
- [ ] Step 2–4: RED → implement → GREEN; lint/typecheck/test/build.
- [ ] Step 5: commit `feat(frontend): ask screen polish, starter questions and e2e for the guided flow`.

### Not a plan task (controller, after merge)

`make deploy-frontend` then check https://jobmatch.ntrye.com in a browser: landing, sign-in, sample → auto Match, Improve, Ask with numbered sources and typing, reload restore, icon in the tab.
