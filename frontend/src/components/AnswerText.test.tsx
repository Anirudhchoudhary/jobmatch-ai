import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { AnswerText } from "./AnswerText";
import type { Citation } from "@/lib/types";

const cite = (label: string, text: string): Citation => ({
  label,
  chunk_id: `id-${label}`,
  doc_type: label.startsWith("r") ? "resume" : "jd",
  section: label.startsWith("r") ? "Summary" : "Requirements",
  text,
});

describe("AnswerText", () => {
  it("turns a known label into a button that reveals the chunk text", async () => {
    const user = userEvent.setup();
    render(<AnswerText text="Built APIs [r1]." citations={[cite("r1", "Built Python APIs at Acme")]} />);
    expect(screen.queryByText("Built Python APIs at Acme")).not.toBeInTheDocument();
    const btn = screen.getByRole("button", { name: "Source 1: Resume · Summary" });
    await user.click(btn);
    expect(screen.getByText("Built Python APIs at Acme")).toBeInTheDocument();
    expect(btn).toHaveAttribute("aria-expanded", "true");
    await user.click(btn);
    expect(screen.queryByText("Built Python APIs at Acme")).not.toBeInTheDocument();
  });

  it("shows numbered superscripts and a Sources row, never the label codes", () => {
    const { container } = render(
      <AnswerText text="Kafka [r1] and Go [j2][r1]." citations={[cite("r1", "a"), cite("j2", "b")]} />,
    );
    const para = container.querySelector("p")!;
    expect(para.textContent).not.toMatch(/[rj]\d/);
    expect(container.querySelectorAll("sup")).toHaveLength(3);
    expect(screen.getByRole("button", { name: "Source 2: Job description · Requirements" })).toBeInTheDocument();
    const row = screen.getByText("Sources").parentElement!;
    expect(row).toHaveTextContent("1 Resume · Summary");
    expect(row).toHaveTextContent("2 Job description · Requirements");
  });

  it("toggles the quote from a Sources chip too", async () => {
    const user = userEvent.setup();
    render(<AnswerText text="Kafka [r1]." citations={[cite("r1", "quote body")]} />);
    await user.click(screen.getByRole("button", { name: "1 Resume · Summary" }));
    expect(screen.getByText("quote body")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "1 Resume · Summary" }));
    expect(screen.queryByText("quote body")).not.toBeInTheDocument();
  });

  it("drops labels missing from citations", () => {
    const { container } = render(<AnswerText text="Claim [r9] and [j2]." citations={[cite("j2", "Needs Go")]} />);
    expect(container.querySelector("p")!.textContent).not.toMatch(/r9|j2/);
    expect(screen.getAllByRole("button", { name: /^Source 1/ })).toHaveLength(1);
  });

  it("hides a half-typed label and shows a cursor while typing", () => {
    const { container } = render(<AnswerText text="Kafka [r" citations={[]} typing />);
    expect(container.querySelector("p")!.textContent).toBe("Kafka ");
    expect(container.querySelector("[data-cursor]")).not.toBeNull();
  });

  it("renders markup in the answer as text, not HTML", () => {
    const { container } = render(<AnswerText text={'<img src=x onerror="alert(1)"> <b>hi</b>'} citations={[]} />);
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("b")).toBeNull();
    expect(container).toHaveTextContent('<img src=x onerror="alert(1)"> <b>hi</b>');
  });
});
