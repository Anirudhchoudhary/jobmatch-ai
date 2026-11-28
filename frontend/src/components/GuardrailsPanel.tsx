"use client";

import { useId, useState } from "react";
import type { GuardrailOutcome, Verdict } from "@/lib/types";

const EXPLAIN: Record<string, string> = {
  injection_rules: "Looks for known prompt-injection phrases that try to change the assistant's instructions.",
  injection_classifier: "A model that scores whether the text is an attempt to hijack the assistant.",
  topic: "Checks the question is about your resume or the job description.",
  pii: "Looks for personal data such as emails, phone numbers or ID numbers.",
  groundedness: "Checks each claim in the answer is supported by the cited resume or job text.",
  bedrock_guardrails: "Amazon Bedrock's managed safety filters (prompt attacks and denied topics).",
};

/** Collapsed-by-default list of every guardrail check behind one answer, plus its verdict. */
export function GuardrailsPanel({ checks, verdict }: { checks: GuardrailOutcome[]; verdict: Verdict | null }) {
  const [open, setOpen] = useState(false);
  const id = useId();

  return (
    <div className="border-t border-border pt-2 text-sm">
      <button
        type="button"
        aria-expanded={open}
        aria-controls={open ? id : undefined}
        onClick={() => setOpen(!open)}
        className="rounded-md px-1 py-0.5 text-xs font-medium text-brand hover:bg-brand-soft focus-visible:outline-2 focus-visible:outline-focus"
      >
        {open ? "Hide guardrails" : "Show guardrails"}
      </button>
      {open ? (
        <div id={id} className="mt-2 flex flex-col gap-2 rounded-lg bg-surface-muted p-3">
          {verdict ? (
            verdict.action === "flag" ? (
              <div>
                <p className="font-medium text-danger">Verdict: flagged. These claims were not supported:</p>
                <ul className="list-disc pl-5">
                  {verdict.unsupported_claims.map((c, i) => (
                    <li key={i}>{c}</li>
                  ))}
                </ul>
              </div>
            ) : (
              <p className="font-medium">Verdict: grounded in your documents.</p>
            )
          ) : null}
          <ul aria-label="Guardrail checks" className="flex flex-col gap-1.5">
            {checks.map((c, i) => {
              const fired = c.action !== "allow";
              return (
                <li
                  key={`${c.guardrail}-${c.stage}-${i}`}
                  data-fired={fired ? "true" : undefined}
                  className={`rounded-md border px-3 py-2 ${fired ? "border-danger/40 bg-danger-soft" : "border-border bg-surface"}`}
                >
                  <p>
                    <span className="font-medium">{c.guardrail}</span>
                    <span className="text-muted">
                      {" "}
                      · {c.provider} · {c.stage} · {c.mode} ·{" "}
                    </span>
                    <span className={fired ? "font-semibold text-danger" : ""}>{c.action}</span>
                  </p>
                  {EXPLAIN[c.guardrail] ? <p className="text-muted">{EXPLAIN[c.guardrail]}</p> : null}
                </li>
              );
            })}
          </ul>
        </div>
      ) : null}
    </div>
  );
}
