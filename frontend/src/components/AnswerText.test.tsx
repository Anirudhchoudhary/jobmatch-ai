import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { AnswerText } from "./AnswerText";
import type { Citation } from "@/lib/types";

const cite = (label: string, text: string): Citation => ({
  label,
  chunk_id: `id-${label}`,
  doc_type: label.startsWith("r") ? "resume" : "jd",
  section: "Experience",
  text,
});

describe("AnswerText", () => {
  it("turns a known label into a button that reveals the chunk text", async () => {
    const user = userEvent.setup();
    render(<AnswerText text="Built APIs [r1]." citations={[cite("r1", "Built Python APIs at Acme")]} />);
    expect(screen.queryByText("Built Python APIs at Acme")).not.toBeInTheDocument();
    const btn = screen.getByRole("button", { name: /r1/ });
    await user.click(btn);
    expect(screen.getByText("Built Python APIs at Acme")).toBeInTheDocument();
    expect(btn).toHaveAttribute("aria-expanded", "true");
    await user.click(btn);
    expect(screen.queryByText("Built Python APIs at Acme")).not.toBeInTheDocument();
  });

  it("keeps labels missing from citations as plain text", () => {
    render(<AnswerText text="Claim [r9] and [j2]." citations={[cite("j2", "Needs Go")]} />);
    expect(screen.queryByRole("button", { name: /r9/ })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /j2/ })).toBeInTheDocument();
    expect(screen.getByText(/Claim \[r9\] and/)).toBeInTheDocument();
  });

  it("renders markup in the answer as text, not HTML", () => {
    const { container } = render(<AnswerText text={'<img src=x onerror="alert(1)"> <b>hi</b>'} citations={[]} />);
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("b")).toBeNull();
    expect(container).toHaveTextContent('<img src=x onerror="alert(1)"> <b>hi</b>');
  });
});
