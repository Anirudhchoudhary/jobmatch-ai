import type { Citation } from "./types";

const LABEL_RE = /\[([rj]\d+)\]/g;

/** Removes `[r1]`/`[j2]` labels from free text (e.g. an unsupported claim) and tidies the spacing. */
export function stripCitationLabels(text: string): string {
  return text.replace(LABEL_RE, "").replace(/\s+([.,;:!?])/g, "$1").replace(/\s{2,}/g, " ").trim();
}

export type AnswerPart = string | { n: number; label: string };

/**
 * Splits answer text into plain strings and numbered citation markers. Numbers follow first appearance,
 * repeated labels reuse their number, and labels with no matching citation are removed.
 */
export function numberCitations(
  text: string,
  citations: Citation[],
): { parts: AnswerPart[]; sources: Array<{ n: number; citation: Citation }> } {
  const byLabel = new Map(citations.map((c) => [c.label, c]));
  const numbers = new Map<string, number>();
  const sources: Array<{ n: number; citation: Citation }> = [];
  const parts: AnswerPart[] = [];
  let last = 0;
  for (const m of text.matchAll(LABEL_RE)) {
    const citation = byLabel.get(m[1]);
    if (m.index > last) parts.push(text.slice(last, m.index));
    last = m.index + m[0].length;
    if (!citation) continue;
    let n = numbers.get(m[1]);
    if (n === undefined) {
      n = numbers.size + 1;
      numbers.set(m[1], n);
      sources.push({ n, citation });
    }
    parts.push({ n, label: m[1] });
  }
  if (last < text.length) parts.push(text.slice(last));
  // Merge strings split by a dropped label so the text stays contiguous.
  return {
    parts: parts.reduce<AnswerPart[]>((acc, p) => {
      const prev = acc[acc.length - 1];
      if (typeof p === "string" && typeof prev === "string") acc[acc.length - 1] = prev + p;
      else acc.push(p);
      return acc;
    }, []),
    sources,
  };
}

/** "Resume · Summary" / "Job description · Requirements" (lowercase sections are title-cased). */
export function sourceName(c: Citation): string {
  const doc = c.doc_type === "resume" ? "Resume" : "Job description";
  const section = c.section === c.section.toLowerCase() ? c.section.replace(/(^|\s)\p{L}/gu, (ch) => ch.toUpperCase()) : c.section;
  return `${doc} · ${section}`;
}
