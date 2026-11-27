"use client";

import { useEffect, useRef, useState, type FormEvent } from "react";
import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { Input } from "@/components/ui/Input";
import { AnswerText } from "@/components/AnswerText";
import { GuardrailsPanel } from "@/components/GuardrailsPanel";
import { GENERIC_ERROR_MESSAGE } from "@/lib/api";
import { streamSse } from "@/lib/sse";
import type { ChatDone, Verdict } from "@/lib/types";

const MAX_QUESTION = 2000; // ChatRequest.question max_length

const STEP_LABELS: Record<string, string> = {
  checking_input: "Checking your question",
  cache_hit: "Found a saved answer",
  retrieving: "Searching your documents",
  reranking: "Ranking the best passages",
  generating: "Writing the answer",
  checking_answer: "Checking the answer against your documents",
};

interface Turn {
  question: string;
  text: string;
  step: string | null;
  verdict: Verdict | null;
  done: ChatDone | null;
  error: string | null;
}

const messageOf = (e: unknown) => (e instanceof Error && e.message ? e.message : GENERIC_ERROR_MESSAGE);

function Badge({ children, tone }: { children: string; tone: "danger" | "muted" }) {
  return (
    <span
      className={`rounded-full border px-2 py-0.5 text-xs font-medium ${
        tone === "danger" ? "border-danger/40 bg-danger-soft text-danger" : "border-border bg-surface-muted text-muted"
      }`}
    >
      {children}
    </span>
  );
}

function TurnView({ turn }: { turn: Turn }) {
  const { done, verdict } = turn;
  const blocked = done?.blocked === true;
  const refused = done?.refused === true && !blocked;
  const flagged = verdict?.action === "flag";
  return (
    <li aria-label="Conversation turn" className="flex flex-col gap-2">
      <p className="self-end rounded-lg bg-surface-muted px-3 py-2 text-sm font-medium">{turn.question}</p>
      <div
        className={`flex flex-col gap-2 rounded-lg border p-3 text-sm ${
          blocked ? "border-danger/40 bg-danger-soft" : refused ? "border-dashed border-border bg-surface-muted" : "border-border"
        }`}
      >
        {blocked ? <Badge tone="danger">Blocked</Badge> : null}
        {refused ? <Badge tone="muted">Not answered</Badge> : null}
        {done?.cached ? <Badge tone="muted">Served from cache</Badge> : null}
        {!done && turn.step && !turn.text ? <p className="text-muted">{STEP_LABELS[turn.step] ?? "Working"}…</p> : null}
        <AnswerText text={done ? done.answer : turn.text} citations={done ? done.citations : []} />
        {flagged && verdict ? (
          <Alert>
            <p className="font-medium">Parts of this answer could not be verified against your documents.</p>
            <ul className="mt-1 list-disc pl-5">
              {verdict.unsupported_claims.map((c, i) => (
                <li key={i}>{c}</li>
              ))}
            </ul>
          </Alert>
        ) : null}
        {turn.error ? <Alert>{turn.error}</Alert> : null}
        {done ? <GuardrailsPanel checks={done.guardrail_checks} verdict={verdict} /> : null}
      </div>
    </li>
  );
}

/** Streaming Q&A over the two documents. History lives in component state only. */
export function Chat() {
  const [question, setQuestion] = useState("");
  const [turns, setTurns] = useState<Turn[]>([]);
  const [busy, setBusy] = useState(false);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => () => abortRef.current?.abort(), []);

  const patchLast = (patch: (t: Turn) => Partial<Turn>) =>
    setTurns((all) => all.map((t, i) => (i === all.length - 1 ? { ...t, ...patch(t) } : t)));

  async function send(e: FormEvent) {
    e.preventDefault();
    const q = question.trim();
    if (!q || busy) return;
    setBusy(true);
    setQuestion("");
    setTurns((all) => [...all, { question: q, text: "", step: null, verdict: null, done: null, error: null }]);
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    try {
      await streamSse(
        "/api/stream/chat",
        { question: q },
        (name, data) => {
          const d = data as Record<string, unknown>;
          if (name === "progress") patchLast(() => ({ step: String(d.step) }));
          else if (name === "token") patchLast((t) => ({ text: t.text + String(d.text ?? "") }));
          else if (name === "verdict") patchLast(() => ({ verdict: data as Verdict }));
          else if (name === "done") patchLast(() => ({ done: data as ChatDone }));
        },
        ctrl.signal,
      );
    } catch (err) {
      if (!ctrl.signal.aborted) patchLast(() => ({ error: messageOf(err) }));
    } finally {
      if (!ctrl.signal.aborted) setBusy(false);
    }
  }

  return (
    <Card className="mt-6 flex flex-col gap-4">
      <h2 className="text-lg font-semibold">Ask about your match</h2>
      <p className="text-sm text-muted">
        Questions are answered only from your resume and the job description. Each answer shows its sources.
      </p>
      {turns.length > 0 ? (
        <ol aria-live="polite" className="flex flex-col gap-4">
          {turns.map((t, i) => (
            <TurnView key={i} turn={t} />
          ))}
        </ol>
      ) : null}
      <form onSubmit={(e) => void send(e)} className="flex flex-col gap-3">
        <Input
          label="Ask a question"
          value={question}
          maxLength={MAX_QUESTION}
          disabled={busy}
          onChange={(e) => setQuestion(e.target.value)}
          placeholder="e.g. Which required skills am I missing?"
        />
        <div>
          <Button type="submit" disabled={busy || !question.trim()}>
            {busy ? "Sending…" : "Send"}
          </Button>
        </div>
      </form>
    </Card>
  );
}
