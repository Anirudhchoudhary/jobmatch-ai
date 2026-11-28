import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { Stepper } from "./Stepper";

const none = { documents: false, match: false, improve: false, ask: false };

describe("Stepper", () => {
  it("is a nav with an ordered list and marks the current step", () => {
    render(
      <Stepper
        step="match"
        unlocked={{ documents: true, match: true, improve: false, ask: false }}
        done={{ ...none, documents: true }}
        onSelect={vi.fn()}
      />,
    );
    const nav = screen.getByRole("navigation", { name: /progress/i });
    expect(nav.querySelector("ol")).not.toBeNull();
    expect(screen.getAllByRole("listitem")).toHaveLength(4);
    expect(screen.getByRole("button", { name: /match/i })).toHaveAttribute("aria-current", "step");
    expect(screen.getByRole("button", { name: /documents/i })).not.toHaveAttribute("aria-current");
    expect(screen.getByRole("button", { name: /documents.*done/i })).toBeEnabled();
  });

  it("disables locked steps, shows their hint and ignores clicks", async () => {
    const onSelect = vi.fn();
    const user = userEvent.setup();
    render(
      <Stepper
        step="documents"
        unlocked={{ documents: true, match: false, improve: false, ask: false }}
        done={none}
        onSelect={onSelect}
      />,
    );
    const match = screen.getByRole("button", { name: /match/i });
    expect(match).toBeDisabled();
    expect(match).toHaveAccessibleDescription(/add both documents first/i);
    expect(screen.getAllByText(/needs your match first/i)).toHaveLength(2);
    await user.click(match);
    expect(onSelect).not.toHaveBeenCalled();
  });

  it("calls onSelect for an unlocked step", async () => {
    const onSelect = vi.fn();
    const user = userEvent.setup();
    render(
      <Stepper
        step="improve"
        unlocked={{ documents: true, match: true, improve: true, ask: true }}
        done={{ ...none, documents: true, match: true }}
        onSelect={onSelect}
      />,
    );
    await user.click(screen.getByRole("button", { name: /ask/i }));
    expect(onSelect).toHaveBeenCalledWith("ask");
  });
});
