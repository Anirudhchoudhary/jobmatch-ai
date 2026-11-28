import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { GuardrailsPanel } from "./GuardrailsPanel";
import type { GuardrailOutcome, Verdict } from "@/lib/types";

const check = (over: Partial<GuardrailOutcome>): GuardrailOutcome => ({
  guardrail: "topic",
  provider: "local",
  stage: "chat_input",
  action: "allow",
  mode: "enforce",
  reason: "ok",
  latency_ms: 1,
  items: [],
  ...over,
});
const grounded: Verdict = { grounded: true, action: "allow", unsupported_claims: [], mode: "enforce" };

describe("GuardrailsPanel", () => {
  it("is collapsed until toggled", async () => {
    const user = userEvent.setup();
    render(<GuardrailsPanel checks={[check({})]} verdict={grounded} />);
    expect(screen.queryByRole("list", { name: /guardrail checks/i })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Show guardrails" }));
    expect(screen.getByRole("list", { name: /guardrail checks/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Hide guardrails" })).toBeInTheDocument();
  });

  it("lists allow and block outcomes, highlighting the non-allow one", async () => {
    const user = userEvent.setup();
    render(
      <GuardrailsPanel
        checks={[
          check({ guardrail: "pii", provider: "regex", action: "allow" }),
          check({ guardrail: "injection_rules", provider: "rules", action: "block", mode: "enforce" }),
        ]}
        verdict={grounded}
      />,
    );
    await user.click(screen.getByRole("button", { name: "Show guardrails" }));
    const items = screen.getAllByRole("listitem");
    expect(items).toHaveLength(2);
    expect(items[0]).toHaveTextContent("pii");
    expect(items[0]).toHaveTextContent("regex");
    expect(items[0]).toHaveTextContent("allow");
    expect(items[0]).not.toHaveAttribute("data-fired");
    expect(items[1]).toHaveTextContent("injection_rules");
    expect(items[1]).toHaveTextContent("block");
    expect(items[1]).toHaveTextContent("chat_input");
    expect(items[1]).toHaveTextContent("enforce");
    expect(items[1]).toHaveAttribute("data-fired", "true");
    expect(items[1]).toHaveTextContent(/prompt-injection/i);
  });

  it("shows the verdict: grounded, or flagged with claims", async () => {
    const user = userEvent.setup();
    const { rerender } = render(<GuardrailsPanel checks={[]} verdict={grounded} />);
    await user.click(screen.getByRole("button", { name: "Show guardrails" }));
    expect(screen.getByText(/grounded in your documents/i)).toBeInTheDocument();
    rerender(
      <GuardrailsPanel
        checks={[]}
        verdict={{ grounded: false, action: "flag", unsupported_claims: ["Led a team of 50"], mode: "enforce" }}
      />,
    );
    expect(screen.getByText(/flagged/i)).toBeInTheDocument();
    expect(screen.getByText("Led a team of 50")).toBeInTheDocument();
  });

  it("only references a controlled region that exists", async () => {
    const user = userEvent.setup();
    render(<GuardrailsPanel checks={[check({})]} verdict={grounded} />);
    const btn = screen.getByRole("button", { name: "Show guardrails" });
    expect(btn).not.toHaveAttribute("aria-controls");
    await user.click(screen.getByRole("button", { name: "Show guardrails" }));
    const id = screen.getByRole("button", { name: "Hide guardrails" }).getAttribute("aria-controls")!;
    expect(document.getElementById(id)).not.toBeNull();
  });

  it("strips citation codes from unsupported claims", async () => {
    const user = userEvent.setup();
    const flag: Verdict = { grounded: false, action: "flag", unsupported_claims: ["Has Go [r2] experience"], mode: "enforce" };
    render(<GuardrailsPanel checks={[check({})]} verdict={flag} />);
    await user.click(screen.getByRole("button", { name: "Show guardrails" }));
    expect(screen.getByText("Has Go experience")).toBeInTheDocument();
  });
});
