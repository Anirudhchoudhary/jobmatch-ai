"use client";

import { useEffect, useRef, useState } from "react";
import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { Badge } from "@/components/ui/Badge";
import { StatusLine, type StatusItem } from "@/components/ui/StatusLine";
import { ChunkViewer } from "@/components/ChunkViewer";
import { ScoreRing } from "@/components/ScoreRing";
import type { StepId } from "@/lib/flow";
import { apiGet, ApiError, GENERIC_ERROR_MESSAGE } from "@/lib/api";
import { streamSse } from "@/lib/sse";
import type { MatchAnalysis, MatchStatus, SkillMatch } from "@/lib/types";

// Backend progress steps in order (`loading` precedes all of them), with friendly labels.
const STEPS: [string, string][] = [
  ["extracting_requirements", "Reading the job"],
  ["extracting_skills", "Reading the resume"],
  ["matching", "Matching skills with evidence"],
  ["validating", "Checking evidence"],
  ["scoring", "Scoring"],
];

function Progress({ step }: { step: string | null }) {
  const reached = STEPS.findIndex(([id]) => id === step);
  const items: StatusItem[] = STEPS.map(([, label], i) => ({
    label,
    state: i < reached ? "done" : i === reached ? "active" : "pending",
  }));
  return <StatusLine items={items} />;
}

const GROUPS: { status: MatchStatus; label: string; dot: string }[] = [
  { status: "matched", label: "Matched", dot: "bg-success" },
  { status: "partial", label: "Partial", dot: "bg-warning" },
  { status: "missing", label: "Missing", dot: "bg-danger" },
];

const pct = (v: number) => `${Math.round(v * 100)}%`;
const messageOf = (e: unknown) => (e instanceof Error && e.message ? e.message : GENERIC_ERROR_MESSAGE);

function Coverage({ label, value }: { label: string; value: number | null }) {
  return (
    <p className="text-sm">
      <span className="text-muted">{label}: </span>
      <span className="font-medium">{value === null ? "n/a" : pct(value)}</span>
    </p>
  );
}

function SkillRow({ match }: { match: SkillMatch }) {
  const [open, setOpen] = useState<number | null>(null);
  const { requirement } = match;
  return (
    <li className="flex flex-col gap-1.5 border-t border-border py-2 first:border-t-0">
      <p className="flex flex-wrap items-center gap-2">
        <span className="font-medium">{requirement.skill}</span>
        <Badge tone={requirement.importance === "required" ? "brand" : "neutral"}>
          {requirement.importance === "required" ? "Required" : "Nice to have"}
        </Badge>
      </p>
      {match.evidence.map((e, i) => (
        <div key={`${e.chunk_id}-${i}`} className="flex flex-col gap-1.5">
          <button
            type="button"
            aria-expanded={open === i}
            onClick={() => setOpen(open === i ? null : i)}
            className="rounded-md border border-border bg-surface-muted px-3 py-2 text-left text-sm focus-visible:outline-2 focus-visible:outline-focus"
          >
            <span className="italic">{e.quote}</span>
            <span aria-hidden="true" className="ml-2 whitespace-nowrap text-brand">
              view in resume
            </span>
          </button>
          {open === i ? <ChunkViewer chunkId={e.chunk_id} quote={e.quote} /> : null}
        </div>
      ))}
    </li>
  );
}

/**
 * Restores the stored analysis on mount; otherwise offers to run one. `docsVersion` changing after
 * mount means the documents were replaced: the old result is hidden and a re-run is offered.
 * A re-run keeps the shown result (and what `onResult` reported) until the new one arrives; null is
 * reported only when no stored result exists or the documents changed. `autoRun` (the user's own action opened this step) starts one run when no current result exists,
 * at most once per `docsVersion`. `onResult` reports the shown analysis id, or null when none.
 */
export function Analysis({
  docsVersion,
  autoRun = false,
  onResult,
  goTo,
}: {
  docsVersion: number;
  autoRun?: boolean;
  goTo?: (step: StepId) => void;
  onResult?: (analysisId: string | null) => void;
}) {
  const [result, setResult] = useState<MatchAnalysis | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [stale, setStale] = useState(false);
  const [running, setRunning] = useState(false);
  const [step, setStep] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const seenVersion = useRef(docsVersion);
  const autoRanFor = useRef<number | null>(null);
  const onResultRef = useRef(onResult);
  useEffect(() => {
    onResultRef.current = onResult;
  });

  useEffect(() => {
    let cancelled = false;
    apiGet<MatchAnalysis>("/api/analyses/latest")
      .then((a) => !cancelled && setResult(a))
      .catch((e: unknown) => {
        if (cancelled || (e instanceof ApiError && e.code === "analysis_not_found")) return;
        setError(messageOf(e));
      })
      .finally(() => !cancelled && setLoaded(true));
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (seenVersion.current !== docsVersion) {
      seenVersion.current = docsVersion;
      setStale(true);
    }
  }, [docsVersion]);

  const shownId = loaded && result !== null && !stale ? result.analysis_id : null;
  useEffect(() => {
    if (loaded) onResultRef.current?.(shownId);
  }, [loaded, shownId]);

  useEffect(() => {
    if (!autoRun || !loaded || running || error || shownId !== null || autoRanFor.current === docsVersion) return;
    autoRanFor.current = docsVersion;
    void run();
  });

  async function run() {
    setRunning(true);
    setError(null);
    setStep(null);
    const startVersion = seenVersion.current;
    try {
      let fresh: MatchAnalysis | null = null;
      await streamSse("/api/stream/analyses", {}, (name, data) => {
        if (name === "progress") setStep((data as { step: string }).step);
        else if (name === "done") fresh = data as MatchAnalysis;
      });
      // Documents replaced mid-run: this result is for the old ones, so keep the re-run offer.
      if (fresh && seenVersion.current === startVersion) {
        setResult(fresh);
        setStale(false);
      }
    } catch (e) {
      setError(messageOf(e));
    } finally {
      setRunning(false);
      setStep(null);
    }
  }

  if (!loaded) {
    return (
      <p role="status" className="mt-4 text-muted">
        Loading…
      </p>
    );
  }

  const showResult = result !== null && !stale;
  const runLabel = result ? "Re-run analysis" : "Run analysis";

  const counts = (s: MatchStatus) => result?.skills.filter((m) => m.status === s).length ?? 0;

  return (
    <div className="mt-4 flex flex-col gap-4">
      {showResult ? null : (
        <Card className="flex flex-col gap-3">
          <h2 className="text-lg font-semibold">Match analysis</h2>
          <p className="text-sm text-muted">
            {stale
              ? "Your documents changed, so the earlier analysis no longer applies."
              : "Compare your resume with the job description."}{" "}
            Running an analysis uses 1 of your 5 daily analyses.
          </p>
          <div>
            <Button onClick={() => void run()} disabled={running}>
              {runLabel}
            </Button>
          </div>
        </Card>
      )}
      {running ? <Progress step={step} /> : null}
      {error ? <Alert>{error}</Alert> : null}
      {showResult ? (
        <>
          <Card className="flex flex-col gap-4">
            <h2 className="text-lg font-semibold">Match analysis</h2>
            <div className="flex items-center gap-5">
              <ScoreRing score={result.score} />
              <div className="flex flex-col gap-1">
                <Coverage label="Required coverage" value={result.required_coverage} />
                <Coverage label="Nice-to-have coverage" value={result.nice_to_have_coverage} />
                <p className="text-sm">
                  <span className="text-success">{counts("matched")} matched</span>
                  {" · "}
                  <span className="text-warning">{counts("partial")} partial</span>
                  {" · "}
                  <span className="text-danger">{counts("missing")} missing</span>
                </p>
              </div>
            </div>
            {result.downgraded_count > 0 ? (
              <Alert tone="info">
                {result.downgraded_count} evidence quote{result.downgraded_count === 1 ? " was" : "s were"} removed because they didn&apos;t match the resume
              </Alert>
            ) : null}
            <div>
              <Button variant="secondary" onClick={() => void run()} disabled={running}>
                Re-run analysis
              </Button>
            </div>
          </Card>
          {GROUPS.map(({ status, label, dot }) => {
            const items = result.skills.filter((s) => s.status === status);
            const headingId = `skills-${status}`;
            return (
              <section key={status} aria-labelledby={headingId}>
                <Card>
                  <h3 id={headingId} className="flex items-center gap-2 text-base font-semibold">
                    <span aria-hidden="true" className={`size-2.5 rounded-full ${dot}`} />
                    {label} ({items.length})
                  </h3>
                  {items.length === 0 ? (
                    <p className="mt-2 text-sm text-muted">None.</p>
                  ) : (
                    <ul className="mt-2">
                      {items.map((m, i) => (
                        <SkillRow key={`${i}-${m.requirement.skill}`} match={m} />
                      ))}
                    </ul>
                  )}
                </Card>
              </section>
            );
          })}
          {goTo ? (
            <div className="flex justify-end">
              <Button onClick={() => goTo("improve")}>
                Next: improve my resume
                <span aria-hidden="true">→</span>
              </Button>
            </div>
          ) : null}
        </>
      ) : null}
    </div>
  );
}
