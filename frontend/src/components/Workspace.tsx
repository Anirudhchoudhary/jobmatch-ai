"use client";

import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { Analysis } from "@/components/Analysis";
import { Chat } from "@/components/Chat";
import { Documents } from "@/components/Documents";
import { Stepper } from "@/components/Stepper";
import { Suggestions } from "@/components/Suggestions";
import { Button } from "@/components/ui/Button";
import { useFlow, type StepId } from "@/lib/flow";

const TITLES: Record<StepId, [string, string]> = {
  documents: ["Step 1 · Your documents", "Add your resume and the job description, or try the sample."],
  match: ["Step 2 · Your match", "How well your resume covers the job, with the evidence for each skill."],
  improve: ["Step 3 · Improve your resume", "Stronger wording for your bullets, grounded in what your resume already says."],
  ask: ["Step 4 · Ask about the fit", "Answers come only from your two documents, with their sources."],
};

function StepPanel({ id, active, children }: { id: StepId; active: boolean; children: ReactNode }) {
  const [title, intro] = TITLES[id];
  return (
    <section hidden={!active} aria-labelledby={`step-${id}-title`} className="flex flex-col gap-4">
      <div>
        <h1 id={`step-${id}-title`} tabIndex={-1} className="text-xl font-semibold tracking-tight focus:outline-none sm:text-2xl">
          {title}
        </h1>
        <p className="mt-1 text-sm text-muted">{intro}</p>
      </div>
      {children}
    </section>
  );
}

function NextButton({ onClick, children }: { onClick(): void; children: ReactNode }) {
  return (
    <div className="flex justify-end">
      <Button onClick={onClick}>
        {children}
        <span aria-hidden="true">→</span>
      </Button>
    </div>
  );
}

/**
 * The guided four-step flow. Every unlocked step stays mounted (inactive ones are `hidden`) so
 * documents, results and the chat history survive moving between steps. Paid runs auto-start
 * only on the active step, and only when the user's own action brought them there.
 */
export function Workspace() {
  const flow = useFlow();
  const { step, arrival, unlocked, done, goTo, onAnalysis } = flow;
  const [analysisId, setAnalysisId] = useState<string | null>(null);

  const onAnalysisResult = useCallback(
    (id: string | null) => {
      setAnalysisId(id);
      onAnalysis(id !== null);
    },
    [onAnalysis],
  );

  // Move focus to the new step's title when the user changes step, so keyboard and screen-reader
  // users land on the new content.
  const lastStep = useRef(step);
  useEffect(() => {
    if (lastStep.current === step) return;
    lastStep.current = step;
    if (arrival === "user") document.getElementById(`step-${step}-title`)?.focus();
  }, [step, arrival]);

  if (flow.restoring) {
    return (
      <p role="status" className="mt-8 text-muted">
        Loading your workspace…
      </p>
    );
  }

  const byUser = arrival === "user";

  return (
    <div className="mt-6 flex flex-col gap-8">
      <Stepper step={step} unlocked={unlocked} done={done} onSelect={goTo} />

      <StepPanel id="documents" active={step === "documents"}>
        <Documents onReady={flow.onDocumentsReady} />
        {flow.revisitingDocuments ? (
          <NextButton onClick={() => goTo("match")}>Continue to your match</NextButton>
        ) : null}
      </StepPanel>

      {unlocked.match ? (
        <StepPanel id="match" active={step === "match"}>
          <Analysis
            docsVersion={flow.docsVersion}
            autoRun={step === "match" && byUser && !done.match}
            onResult={onAnalysisResult}
            goTo={goTo}
          />
        </StepPanel>
      ) : null}

      {unlocked.improve ? (
        <StepPanel id="improve" active={step === "improve"}>
          {analysisId ? (
            <Suggestions
              key={analysisId}
              autoRun={step === "improve" && byUser && !done.improve}
              onResult={flow.onSuggestions}
              goTo={goTo}
            />
          ) : (
            <p role="status" className="text-muted">
              Loading…
            </p>
          )}
        </StepPanel>
      ) : null}

      {/* Kept mounted (hidden) while open so the conversation survives moving between steps. */}
      {unlocked.ask ? (
        <StepPanel id="ask" active={step === "ask"}>
          <Chat />
        </StepPanel>
      ) : null}
    </div>
  );
}
