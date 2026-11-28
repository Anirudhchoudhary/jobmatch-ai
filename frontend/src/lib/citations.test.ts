import { describe, expect, it } from "vitest";
import { numberCitations, sourceName, stripCitationLabels } from "./citations";
import type { Citation } from "./types";

const cite = (label: string, section = "Summary"): Citation => ({
  label,
  chunk_id: `id-${label}`,
  doc_type: label.startsWith("r") ? "resume" : "jd",
  section,
  text: `text ${label}`,
});

describe("numberCitations", () => {
  it("numbers labels by first appearance and reuses numbers for repeats", () => {
    const { parts, sources } = numberCitations("A [j2] B [r1] C [j2].", [cite("r1"), cite("j2")]);
    expect(parts).toEqual(["A ", { n: 1, label: "j2" }, " B ", { n: 2, label: "r1" }, " C ", { n: 1, label: "j2" }, "."]);
    expect(sources.map((s) => [s.n, s.citation.label])).toEqual([
      [1, "j2"],
      [2, "r1"],
    ]);
  });

  it("drops unknown labels from the text", () => {
    const { parts, sources } = numberCitations("Claim [r9] done.", [cite("r1")]);
    expect(parts.join("")).toBe("Claim  done.");
    expect(sources).toEqual([]);
  });

  it("keeps adjacent labels as separate numbers with no label letters", () => {
    const { parts } = numberCitations("Skills [r1][r3].", [cite("r1"), cite("r3", "Skills")]);
    expect(parts).toEqual(["Skills ", { n: 1, label: "r1" }, { n: 2, label: "r3" }, "."]);
    expect(parts.filter((p) => typeof p === "string").join("")).not.toMatch(/[rj]\d/);
  });

  it("returns the plain text untouched when there are no labels", () => {
    expect(numberCitations("Just text", []).parts).toEqual(["Just text"]);
  });
});

describe("sourceName", () => {
  it("names the document type and title-cases lowercase sections", () => {
    expect(sourceName(cite("r1", "work experience"))).toBe("Resume · Work Experience");
    expect(sourceName(cite("r1", "candidate's skills"))).toBe("Resume · Candidate's Skills");
    expect(sourceName(cite("r1", "node.js"))).toBe("Resume · Node.js");
    expect(sourceName(cite("j1", "Requirements"))).toBe("Job description · Requirements");
  });
});

describe("stripCitationLabels", () => {
  it("removes labels and markdown bold and tidies spacing", () => {
    expect(stripCitationLabels("**Kafka:** the role asks for it [j2] , and you have it [r1].")).toBe(
      "Kafka: the role asks for it, and you have it.",
    );
  });
});
