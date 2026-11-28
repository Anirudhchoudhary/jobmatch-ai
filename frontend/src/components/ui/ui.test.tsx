import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Badge } from "./Badge";
import { Button } from "./Button";
import { Card } from "./Card";
import { Logo } from "./Logo";
import { StatusLine } from "./StatusLine";

describe("Button", () => {
  it("is disabled and shows a labelled spinner while loading", () => {
    render(<Button loading>Save</Button>);
    const button = screen.getByRole("button", { name: /save/i });
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute("aria-busy", "true");
    expect(screen.getByRole("status", { name: /loading/i })).toBeInTheDocument();
  });

  it("is enabled with no spinner by default", () => {
    render(<Button variant="ghost">Go</Button>);
    expect(screen.getByRole("button", { name: "Go" })).toBeEnabled();
    expect(screen.queryByRole("status")).toBeNull();
  });
});

describe("Card", () => {
  it("renders an optional title and actions", () => {
    render(
      <Card title="Match" actions={<button>Re-run</button>}>
        body
      </Card>,
    );
    expect(screen.getByRole("heading", { name: "Match" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Re-run" })).toBeInTheDocument();
    expect(screen.getByText("body")).toBeInTheDocument();
  });
});

describe("Badge", () => {
  it.each(["neutral", "brand", "success", "warning", "danger"] as const)("renders the %s tone", (tone) => {
    render(<Badge tone={tone}>{tone} label</Badge>);
    expect(screen.getByText(`${tone} label`)).toHaveAttribute("data-tone", tone);
  });
});

describe("StatusLine", () => {
  it("marks the active item and announces through a polite live region", () => {
    render(
      <StatusLine
        items={[
          { label: "Checked", state: "done" },
          { label: "Searching", state: "active" },
          { label: "Verifying", state: "pending" },
        ]}
      />,
    );
    const region = screen.getByRole("status");
    expect(region).toHaveAttribute("aria-live", "polite");
    expect(screen.getByText("Searching").closest("li")).toHaveAttribute("aria-current", "step");
    expect(screen.getByText("Checked").closest("li")).toHaveAttribute("data-state", "done");
    expect(screen.getByText("Verifying").closest("li")).toHaveAttribute("data-state", "pending");
  });
});

describe("Logo", () => {
  it("renders a decorative mark and the product name", () => {
    const { container } = render(<Logo />);
    expect(container.querySelector("svg")).toHaveAttribute("aria-hidden", "true");
    expect(screen.getByText("JobMatch AI")).toBeInTheDocument();
  });
});
