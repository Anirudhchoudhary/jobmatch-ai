"use client";

import { useEffect, useState } from "react";
import { Alert } from "@/components/ui/Alert";
import { apiGet, GENERIC_ERROR_MESSAGE } from "@/lib/api";
import type { ChunkText } from "@/lib/types";

/** Split `text` around the first case-insensitive occurrence of `quote`; null when absent. */
function splitAtQuote(text: string, quote: string): [string, string, string] | null {
  if (!quote) return null;
  const m = new RegExp(quote.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "i").exec(text);
  if (!m) return null;
  return [text.slice(0, m.index), m[0], text.slice(m.index + m[0].length)];
}

/** Fetches one chunk and shows it with the evidence quote highlighted (or quoted above it). */
export function ChunkViewer({ chunkId, quote }: { chunkId: string; quote: string }) {
  const [chunk, setChunk] = useState<ChunkText | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    apiGet<ChunkText>(`/api/chunks/${chunkId}`)
      .then((c) => !cancelled && setChunk(c))
      .catch((e: unknown) => !cancelled && setError(e instanceof Error && e.message ? e.message : GENERIC_ERROR_MESSAGE));
    return () => {
      cancelled = true;
    };
  }, [chunkId]);

  if (error) return <Alert>{error}</Alert>;
  if (!chunk) return <p role="status" className="text-sm text-muted">Loading section…</p>;

  const parts = splitAtQuote(chunk.text, quote);
  return (
    <div className="rounded-md border border-border bg-surface-muted p-3 text-sm">
      <p className="mb-2 font-medium text-muted">
        {chunk.doc_type === "resume" ? "Resume" : "Job description"} · {chunk.section}
      </p>
      {parts ? null : (
        <p className="mb-2">
          <span className="text-muted">Quote not found in this section: </span>
          <q>{quote}</q>
        </p>
      )}
      <p className="whitespace-pre-wrap">
        {parts ? (
          <>
            {parts[0]}
            <mark className="rounded bg-warning-soft px-0.5 font-medium text-foreground">{parts[1]}</mark>
            {parts[2]}
          </>
        ) : (
          chunk.text
        )}
      </p>
    </div>
  );
}
