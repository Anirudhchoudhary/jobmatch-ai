"use client";

import { Fragment, useState } from "react";
import { numberCitations, sourceName } from "@/lib/citations";
import { plainText, richParts } from "@/lib/markdown";
import type { Citation } from "@/lib/types";

const FOCUS = "focus-visible:outline-2 focus-visible:outline-focus";

/**
 * Answer text as plain React text. `[r1]`/`[j2]` labels that match a citation become numbered
 * superscript buttons (numbered by first appearance) that reveal the cited chunk; labels without a
 * citation are dropped. A half-typed label at the end is hidden while typing. Light markdown (bold,
 * bullets) is styled via `richParts`. Nothing is parsed as HTML.
 */
export function AnswerText({
  text,
  citations,
  typing = false,
}: {
  text: string;
  citations: Citation[];
  typing?: boolean;
}) {
  const [open, setOpen] = useState<number | null>(null);
  const visible = typing ? text.replace(/\[[rj]?\d*$/, "") : text;
  const { parts, sources } = numberCitations(visible, citations);
  const opened = sources.find((s) => s.n === open)?.citation;
  const toggle = (n: number) => setOpen(open === n ? null : n);

  return (
    <div className="flex flex-col gap-2">
      <p aria-live={typing ? "off" : undefined} className="whitespace-pre-wrap [overflow-wrap:anywhere]">
        {richParts(parts).map((part, i) =>
          "text" in part ? (
            part.bold ? (
              <strong key={i} className="font-semibold">
                {part.text}
              </strong>
            ) : (
              <Fragment key={i}>{part.text}</Fragment>
            )
          ) : (
            <sup key={i}>
              <button
                type="button"
                aria-expanded={open === part.n}
                aria-label={`Source ${part.n}: ${sourceName(sources[part.n - 1].citation)}`}
                onClick={() => toggle(part.n)}
                className={`mx-px select-none -my-1 rounded px-1.5 py-1 font-bold text-brand hover:underline ${FOCUS}`}
              >
                {part.n}
              </button>
            </sup>
          ),
        )}
        {typing ? (
          <span
            data-cursor
            aria-hidden="true"
            className="jm-cursor ml-0.5 inline-block h-[1em] w-[2px] translate-y-[2px] bg-brand"
          />
        ) : null}
      </p>
      <div data-testid="answer-announce" role="status" aria-live="polite" className="sr-only">
        {typing ? "" : plainText(parts)}
      </div>
      {sources.length > 0 ? (
        <div className="flex select-none flex-wrap items-center gap-1.5 border-t border-border pt-2 text-xs text-muted">
          <span>Sources</span>
          {sources.map(({ n, citation }) => (
            <button
              key={n}
              type="button"
              aria-expanded={open === n}
              onClick={() => toggle(n)}
              className={`rounded-md bg-brand-soft px-1.5 py-0.5 text-foreground hover:underline ${FOCUS}`}
            >
              {n} {sourceName(citation)}
            </button>
          ))}
        </div>
      ) : null}
      {opened ? (
        <blockquote className="rounded-md border border-border bg-surface-muted p-3 text-sm">
          <p className="mb-1 font-medium text-muted">{sourceName(opened)}</p>
          <p aria-live={typing ? "off" : undefined} className="whitespace-pre-wrap [overflow-wrap:anywhere]">{opened.text}</p>
        </blockquote>
      ) : null}
    </div>
  );
}
