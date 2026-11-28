"use client";

import { useEffect, useRef, useState } from "react";
import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { Badge } from "@/components/ui/Badge";
import { Card } from "@/components/ui/Card";
import { StatusLine, type StatusItem } from "@/components/ui/StatusLine";
import type { StepId } from "@/lib/flow";
import { apiGet, ApiError, GENERIC_ERROR_MESSAGE } from "@/lib/api";
import { streamSse } from "@/lib/sse";
import type { BulletSuggestions } from "@/lib/types";

// Backend progress steps in order (`loading` precedes all of them), with friendly labels.
const STEPS: [string, string][] = [
  ["selecting", "Choosing bullets to improve"],
  ["rewriting", "Rewriting"],
  ["validating", "Checking against your resume"],
];

function Progress({ step }: { step: string | null }) {
  const reached = STEPS.findIndex(([id]) => id === step);
  const items: StatusItem[] = STEPS.map(([, label], i) => ({
    label,
    state: i < reached ? "done" : i === reached ? "active" : "pending",
  }));
  return <StatusLine items={items} />;
}

/**
 * Restores stored rewrites on mount; "Suggest rewrites" spends a daily allowance. `autoRun` (the
 * user's own action opened this step) starts one run when none are stored. `onResult` reports
 * whether rewrites are shown.
 */
export function Suggestions({
  autoRun = false,
  onResult,
  goTo,
}: {
  autoRun?: boolean;
  onResult?: (present: boolean) => void;
  goTo?: (step: StepId) => void;
} = {}) {
  const [result, setResult] = useState<BulletSuggestions | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [step, setStep] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const autoRan = useRef(false);
  const onResultRef = useRef(onResult);
  useEffect(() => {
    onResultRef.current = onResult;
  });

  useEffect(() => {
    let cancelled = false;
    apiGet<BulletSuggestions>("/api/suggestions/latest")
      .then((s) => !cancelled && setResult(s))
      .catch((e: unknown) => {
        if (cancelled || (e instanceof ApiError && e.code === "suggestions_not_found")) return;
        setError(e instanceof Error && e.message ? e.message : GENERIC_ERROR_MESSAGE);
      })
      .finally(() => !cancelled && setLoaded(true));
    return () => {
      cancelled = true;
    };
  }, []);

  const present = result !== null;
  useEffect(() => {
    if (loaded) onResultRef.current?.(present);
  }, [loaded, present]);

  useEffect(() => {
    if (!autoRun || !loaded || busy || error || present || autoRan.current) return;
    autoRan.current = true;
    void generate();
  });

  async function generate() {
    setBusy(true);
    setError(null);
    setStep(null);
    try {
      let fresh: BulletSuggestions | null = null;
      await streamSse("/api/stream/suggestions", {}, (name, data) => {
        if (name === "progress") setStep((data as { step: string }).step);
        else if (name === "done") fresh = data as BulletSuggestions;
      });
      if (fresh) setResult(fresh);
    } catch (e) {
      setError(e instanceof Error && e.message ? e.message : GENERIC_ERROR_MESSAGE);
    } finally {
      setBusy(false);
      setStep(null);
    }
  }

  if (!loaded) return null;

  return (
    <div className="flex flex-col gap-4">
      <Card className="flex flex-col gap-4">
        <h2 className="text-lg font-semibold">Resume rewrites</h2>
        {result ? null : (
          <p className="text-sm text-muted">
            Get stronger wording for resume bullets, grounded in what your resume already says. Uses 1 daily suggestion run.
          </p>
        )}
        <div>
          <Button variant={result ? "secondary" : "primary"} onClick={() => void generate()} disabled={busy}>
            {result ? "Suggest again" : "Suggest rewrites"}
          </Button>
        </div>
        {busy ? <Progress step={step} /> : null}
        {error ? <Alert>{error}</Alert> : null}
        {result ? (
          <>
            {result.suggestions.length === 0 ? <p className="text-sm text-muted">No suggestions this time.</p> : null}
            <ul className="flex flex-col gap-4">
              {result.suggestions.map((s, i) => (
                <li key={`${s.original_chunk_id}-${i}`} className="flex flex-col gap-2 border-t border-border pt-4 first:border-t-0 first:pt-0">
                  <div className="grid gap-3 sm:grid-cols-2">
                    <div className="rounded-lg border border-border bg-surface-muted p-3 text-sm text-muted">
                      <p className="mb-1 font-semibold text-foreground">Before</p>
                      <p>{s.original}</p>
                    </div>
                    <div className="rounded-lg border border-success/30 bg-success-soft p-3 text-sm">
                      <p className="mb-1 font-semibold">After</p>
                      <p>{s.rewritten}</p>
                    </div>
                  </div>
                  <p className="text-sm text-muted">
                    <span className="font-medium text-foreground">Why: </span>
                    {s.rationale}
                  </p>
                  {s.target_skills.length > 0 ? (
                    <ul aria-label="Target skills" className="flex flex-wrap gap-1.5">
                      {s.target_skills.map((t) => (
                        <li key={t}>
                          <Badge tone="brand">{t}</Badge>
                        </li>
                      ))}
                    </ul>
                  ) : null}
                </li>
              ))}
            </ul>
            {result.rejected_count > 0 ? (
              <p className="text-sm text-muted">
                {result.rejected_count} rewrite{result.rejected_count === 1 ? "" : "s"} blocked for adding facts not in your resume.
              </p>
            ) : null}
          </>
        ) : null}
      </Card>
      {goTo ? (
        <div className="flex justify-end">
          <Button variant={result ? "primary" : "secondary"} onClick={() => goTo("ask")}>
            Next: ask questions
            <span aria-hidden="true">→</span>
          </Button>
        </div>
      ) : null}
    </div>
  );
}
