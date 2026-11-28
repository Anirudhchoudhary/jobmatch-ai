"use client";

import { useCallback, useEffect, useState } from "react";
import { apiGet } from "./api";
import type { DocumentList } from "./types";

export type StepId = "documents" | "match" | "improve" | "ask";
export const STEPS: readonly StepId[] = ["documents", "match", "improve", "ask"];

/** How the user reached the current step: their own action, or a page reload restoring it. */
export type Arrival = "user" | "restore";

interface FlowState {
  step: StepId;
  arrival: Arrival;
  restoring: boolean;
  docsReady: boolean;
  hasAnalysis: boolean;
  hasSuggestions: boolean;
  /** Bumped each time a document is added or replaced (not when the stored pair is reported). */
  docsVersion: number;
}

export interface Flow {
  step: StepId;
  arrival: Arrival;
  restoring: boolean;
  docsVersion: number;
  unlocked: Record<StepId, boolean>;
  done: Record<StepId, boolean>;
  /** On Step 1 with both documents ready: show "Continue to your match" instead of moving on. */
  revisitingDocuments: boolean;
  goTo(step: StepId): void;
  onDocumentsReady(changed: boolean): void;
  onAnalysis(present: boolean): void;
  onSuggestions(present: boolean): void;
}

const INITIAL: FlowState = {
  step: "documents",
  arrival: "restore",
  restoring: true,
  docsReady: false,
  hasAnalysis: false,
  hasSuggestions: false,
  docsVersion: 0,
};

const unlockedOf = (s: FlowState): Record<StepId, boolean> => ({
  documents: true,
  match: s.docsReady,
  improve: s.docsReady && s.hasAnalysis,
  ask: s.docsReady && s.hasAnalysis,
});

/** Keeps the flow on an open step: if the current step became locked, fall back to the furthest open one. */
function settle(s: FlowState): FlowState {
  const open = unlockedOf(s);
  if (open[s.step]) return s;
  const step = [...STEPS].reverse().find((id) => open[id]) ?? "documents";
  return { ...s, step };
}

const exists = (path: string) =>
  apiGet<unknown>(path).then(
    () => true,
    () => false,
  );

const bothReady = ({ documents }: DocumentList) =>
  ["resume", "jd"].every((t) => documents.some((d) => d.doc_type === t));

/**
 * The guided flow's state. Paid runs (analysis, suggestions) may start automatically only when
 * `arrival === "user"`; a reload restores the furthest step the stored data supports with
 * `arrival: "restore"`, which never auto-runs.
 */
export function useFlow(): Flow {
  const [s, setS] = useState<FlowState>(INITIAL);

  useEffect(() => {
    let cancelled = false;
    void Promise.all([
      apiGet<DocumentList>("/api/documents").then(bothReady, () => false),
      exists("/api/analyses/latest"),
      exists("/api/suggestions/latest"),
    ]).then(([docsReady, analysis, suggestions]) => {
      if (cancelled) return;
      const hasAnalysis = docsReady && analysis;
      const hasSuggestions = hasAnalysis && suggestions;
      const step: StepId = hasSuggestions ? "ask" : hasAnalysis ? "improve" : docsReady ? "match" : "documents";
      setS((prev) => ({ ...prev, step, arrival: "restore", restoring: false, docsReady, hasAnalysis, hasSuggestions }));
    });
    return () => {
      cancelled = true;
    };
  }, []);

  const goTo = useCallback((step: StepId) => {
    // Re-selecting the current step is not a new arrival: it must not turn a restore into a paid auto-run.
    setS((prev) => (step !== prev.step && unlockedOf(prev)[step] ? { ...prev, step, arrival: "user" } : prev));
  }, []);

  const onDocumentsReady = useCallback((changed: boolean) => {
    setS((prev) => {
      // Documents reporting the pair that was already stored: nothing new happened.
      if (!changed) return prev.docsReady ? prev : { ...prev, docsReady: true };
      const firstCompletion = !prev.docsReady && prev.step === "documents";
      return settle({
        ...prev,
        docsReady: true,
        // A new or replaced document makes any earlier match and rewrites stale.
        hasAnalysis: false,
        hasSuggestions: false,
        docsVersion: prev.docsVersion + 1,
        ...(firstCompletion ? { step: "match" as const, arrival: "user" as const } : {}),
      });
    });
  }, []);

  const onAnalysis = useCallback((present: boolean) => {
    setS((prev) =>
      prev.hasAnalysis === present
        ? prev
        : settle({ ...prev, hasAnalysis: present, hasSuggestions: present && prev.hasSuggestions }),
    );
  }, []);

  const onSuggestions = useCallback((present: boolean) => {
    setS((prev) => (prev.hasSuggestions === present ? prev : { ...prev, hasSuggestions: present }));
  }, []);

  const unlocked = unlockedOf(s);
  return {
    step: s.step,
    arrival: s.arrival,
    restoring: s.restoring,
    docsVersion: s.docsVersion,
    unlocked,
    done: {
      documents: s.docsReady,
      match: unlocked.improve,
      improve: unlocked.improve && s.hasSuggestions,
      ask: false,
    },
    revisitingDocuments: s.step === "documents" && s.docsReady,
    goTo,
    onDocumentsReady,
    onAnalysis,
    onSuggestions,
  };
}
