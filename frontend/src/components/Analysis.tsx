"use client";

import { useEffect, useRef, useState } from "react";
import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { ChunkViewer } from "@/components/ChunkViewer";
import { Suggestions } from "@/components/Suggestions";
import { apiGet, ApiError, GENERIC_ERROR_MESSAGE } from "@/lib/api";
import { streamSse } from "@/lib/sse";
import type { MatchAnalysis, MatchStatus, SkillMatch } from "@/lib/types";

const STEP_LABELS: Record<string, string> = {
  loading: "Loading your documents",
  extracting_requirements: "Finding the job requirements",
  extracting_skills: "Finding skills in your resume",
  matching: "Matching skills to requirements",
  validating: "Checking the evidence",
  scoring: "Scoring",
};

const GROUPS: { status: MatchStatus; label: string }[] = [
  { status: "matched", label: "Matched" },
  { status: "partial", label: "Partial" },
  { status: "missing", label: "Missing" },
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

function ScoreBar({ score }: { score: number }) {
  return (
    <div className="flex items-center gap-4">
      <p className="text-5xl font-semibold tabular-nums">{score}</p>
      <div className="flex flex-1 flex-col gap-1">
        <span className="text-sm text-muted">Match score out of 100</span>
        <div
          role="meter"
          aria-label="Match score"
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={score}
          className="h-3 w-full overflow-hidden rounded-full bg-surface-muted"
        >
          <div className="h-full bg-brand" style={{ width: `${score}%` }} />
        </div>
      </div>
    </div>
  );
}

function SkillRow({ match }: { match: SkillMatch }) {
  const [open, setOpen] = useState<number | null>(null);
  const { requirement } = match;
  return (
    <li className="flex flex-col gap-1.5 border-t border-border py-2 first:border-t-0">
      <p>
        <span className="font-medium">{requirement.skill}</span>
        <span className="text-sm text-muted">
          {" "}
          · {requirement.importance === "required" ? "Required" : "Nice to have"}
        </span>
      </p>
      {match.evidence.map((e, i) => (
        <div key={`${e.chunk_id}-${i}`} className="flex flex-col gap-1.5">
          <button
            type="button"
            aria-expanded={open === i}
            onClick={() => setOpen(open === i ? null : i)}
            className="text-left text-sm italic text-brand underline-offset-2 hover:underline focus-visible:outline-2 focus-visible:outline-focus"
          >
            {e.quote}
          </button>
          {open === i ? <ChunkViewer chunkId={e.chunk_id} quote={e.quote} /> : null}
        </div>
      ))}
    </li>
  );
}

/**
 * Restores the stored analysis on mount; otherwise offers to run one. `docsVersion` changing after
 * mount means the documents were replaced: the old result is hidden and a re-run is offered
 * (never run automatically, it spends a daily analysis).
 */
export function Analysis({ docsVersion }: { docsVersion: number }) {
  const [result, setResult] = useState<MatchAnalysis | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [stale, setStale] = useState(false);
  const [running, setRunning] = useState(false);
  const [step, setStep] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const seenVersion = useRef(docsVersion);

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

  async function run() {
    setRunning(true);
    setError(null);
    setStep(null);
    try {
      let fresh: MatchAnalysis | null = null;
      await streamSse("/api/stream/analyses", {}, (name, data) => {
        if (name === "progress") setStep((data as { step: string }).step);
        else if (name === "done") fresh = data as MatchAnalysis;
      });
      if (fresh) {
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
      {running && step ? (
        <p role="status" className="text-sm font-medium">
          … {STEP_LABELS[step] ?? "Working"}
        </p>
      ) : null}
      {error ? <Alert>{error}</Alert> : null}
      {showResult ? (
        <>
          <Card className="flex flex-col gap-4">
            <h2 className="text-lg font-semibold">Match analysis</h2>
            <ScoreBar score={result.score} />
            <div className="flex flex-wrap gap-x-6 gap-y-1">
              <Coverage label="Required coverage" value={result.required_coverage} />
              <Coverage label="Nice-to-have coverage" value={result.nice_to_have_coverage} />
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
          {GROUPS.map(({ status, label }) => {
            const items = result.skills.filter((s) => s.status === status);
            const headingId = `skills-${status}`;
            return (
              <section key={status} aria-labelledby={headingId}>
                <Card>
                  <h3 id={headingId} className="text-base font-semibold">
                    {label} ({items.length})
                  </h3>
                  {items.length === 0 ? (
                    <p className="mt-2 text-sm text-muted">None.</p>
                  ) : (
                    <ul className="mt-2">
                      {items.map((m) => (
                        <SkillRow key={m.requirement.skill} match={m} />
                      ))}
                    </ul>
                  )}
                </Card>
              </section>
            );
          })}
          <Suggestions key={result.analysis_id} />
        </>
      ) : null}
    </div>
  );
}
