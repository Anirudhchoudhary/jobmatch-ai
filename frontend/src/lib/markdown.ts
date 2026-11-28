import type { AnswerPart } from "./citations";

/** A run of answer text: plain or bold (from markdown `**`), or a numbered citation marker. */
export type RichPart = { text: string; bold: boolean } | { n: number; label: string };

/** Line-start markdown the model sometimes writes: "- " / "* " bullets and "#" headings. */
function tidyLines(text: string, atLineStart: boolean): string {
  const lines = text.split("\n");
  return lines
    .map((line, i) => {
      if (i === 0 && !atLineStart) return line;
      return line.replace(/^(\s*)[-*]\s+/, "$1• ").replace(/^#{1,6}\s+/, "");
    })
    .join("\n");
}

/**
 * Turns the light markdown models write into styled runs, without parsing any HTML: `**bold**`
 * (which may span a citation marker) becomes bold runs, bullets become "• ", and heading marks
 * are dropped. An unclosed `**` while typing simply bolds the rest until it closes.
 */
export function richParts(parts: AnswerPart[]): RichPart[] {
  const out: RichPart[] = [];
  let bold = false;
  let atLineStart = true;
  for (const part of parts) {
    if (typeof part !== "string") {
      out.push(part);
      atLineStart = false;
      continue;
    }
    const text = tidyLines(part, atLineStart);
    text.split("**").forEach((piece, i) => {
      if (i > 0) bold = !bold;
      if (piece) out.push({ text: piece, bold });
    });
    atLineStart = text.endsWith("\n");
  }
  return out;
}

/** The answer as plain readable text (for screen-reader announcements). */
export function plainText(parts: AnswerPart[]): string {
  return richParts(parts)
    .map((p) => ("text" in p ? p.text : ""))
    .join("");
}
