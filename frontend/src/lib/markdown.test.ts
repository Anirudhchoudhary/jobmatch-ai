import { describe, expect, it } from "vitest";
import { plainText, richParts } from "./markdown";

describe("richParts", () => {
  it("turns **bold** into bold runs", () => {
    expect(richParts(["Your gap is **Kubernetes** today."])).toEqual([
      { text: "Your gap is ", bold: false },
      { text: "Kubernetes", bold: true },
      { text: " today.", bold: false },
    ]);
  });

  it("keeps bold across a citation marker", () => {
    expect(richParts(["**Python ", { n: 1, label: "r1" }, " and FastAPI** shown"])).toEqual([
      { text: "Python ", bold: true },
      { n: 1, label: "r1" },
      { text: " and FastAPI", bold: true },
      { text: " shown", bold: false },
    ]);
  });

  it("turns line-start dashes into bullets and drops heading marks", () => {
    expect(plainText(["## Summary\n- one\n* two\nnot - a bullet"])).toBe("Summary\n• one\n• two\nnot - a bullet");
  });

  it("does not treat a dash after a citation on the same line as a bullet", () => {
    expect(plainText(["a ", { n: 1, label: "r1" }, " - b"])).toBe("a  - b");
  });

  it("bolds the rest while a ** is still open (typing)", () => {
    expect(richParts(["lead **Kafk"])).toEqual([
      { text: "lead ", bold: false },
      { text: "Kafk", bold: true },
    ]);
  });
});
