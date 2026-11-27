"use client";

import { Fragment, useState } from "react";
import type { Citation } from "@/lib/types";

const LABEL_RE = /\[([rj]\d+)\]/g;

/**
 * Answer text as plain React text. `[r1]`/`[j2]` labels that match a citation become buttons
 * that reveal the cited chunk; any other label stays as text. Nothing is parsed as HTML.
 */
export function AnswerText({ text, citations }: { text: string; citations: Citation[] }) {
  const [open, setOpen] = useState<string | null>(null);
  const byLabel = new Map(citations.map((c) => [c.label, c]));
  const parts = text.split(LABEL_RE); // odd indexes are captured labels
  const opened = open ? byLabel.get(open) : undefined;

  return (
    <div className="flex flex-col gap-2">
      <p className="whitespace-pre-wrap">
        {parts.map((part, i) => {
          if (i % 2 === 0) return <Fragment key={i}>{part}</Fragment>;
          if (!byLabel.has(part)) return <Fragment key={i}>{`[${part}]`}</Fragment>;
          return (
            <button
              key={i}
              type="button"
              aria-expanded={open === part}
              aria-label={`Source ${part}`}
              onClick={() => setOpen(open === part ? null : part)}
              className="mx-0.5 rounded border border-border bg-surface-muted px-1 text-xs font-medium text-brand hover:underline focus-visible:outline-2 focus-visible:outline-focus"
            >
              {part}
            </button>
          );
        })}
      </p>
      {opened ? (
        <blockquote className="rounded-md border border-border bg-surface-muted p-3 text-sm">
          <p className="mb-1 font-medium text-muted">
            {opened.doc_type === "resume" ? "Resume" : "Job description"} · {opened.section}
          </p>
          <p className="whitespace-pre-wrap">{opened.text}</p>
        </blockquote>
      ) : null}
    </div>
  );
}
