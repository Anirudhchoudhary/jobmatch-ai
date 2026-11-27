"use client";

import { useEffect, useState } from "react";
import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { apiGet, apiPost, ApiError, GENERIC_ERROR_MESSAGE } from "@/lib/api";
import type { BulletSuggestions } from "@/lib/types";

/** Restores stored rewrites on mount; "Suggest rewrites" spends a daily allowance. */
export function Suggestions() {
  const [result, setResult] = useState<BulletSuggestions | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

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

  async function generate() {
    setBusy(true);
    setError(null);
    try {
      setResult(await apiPost<BulletSuggestions>("/api/suggestions", {}));
    } catch (e) {
      setError(e instanceof Error && e.message ? e.message : GENERIC_ERROR_MESSAGE);
    } finally {
      setBusy(false);
    }
  }

  if (!loaded) return null;

  return (
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
      {error ? <Alert>{error}</Alert> : null}
      {result ? (
        <>
          {result.suggestions.length === 0 ? <p className="text-sm text-muted">No suggestions this time.</p> : null}
          <ul className="flex flex-col gap-4">
            {result.suggestions.map((s, i) => (
              <li key={`${s.original_chunk_id}-${i}`} className="flex flex-col gap-2 border-t border-border pt-4 first:border-t-0 first:pt-0">
                <div className="grid gap-3 sm:grid-cols-2">
                  <div className="rounded-md border border-border bg-surface-muted p-3 text-sm">
                    <p className="mb-1 font-medium text-muted">Original</p>
                    <p>{s.original}</p>
                  </div>
                  <div className="rounded-md border border-border p-3 text-sm">
                    <p className="mb-1 font-medium text-muted">Rewritten</p>
                    <p>{s.rewritten}</p>
                  </div>
                </div>
                <p className="text-sm">{s.rationale}</p>
                {s.target_skills.length > 0 ? (
                  <ul aria-label="Target skills" className="flex flex-wrap gap-1.5">
                    {s.target_skills.map((t) => (
                      <li key={t} className="rounded-full border border-border bg-surface-muted px-2 py-0.5 text-xs">
                        {t}
                      </li>
                    ))}
                  </ul>
                ) : null}
              </li>
            ))}
          </ul>
          {result.rejected_count > 0 ? (
            <p className="text-sm text-muted">
              {result.rejected_count} suggestion{result.rejected_count === 1 ? " was" : "s were"} rejected because they weren&apos;t grounded in your resume.
            </p>
          ) : null}
        </>
      ) : null}
    </Card>
  );
}
