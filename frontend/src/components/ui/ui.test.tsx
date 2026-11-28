import { readFileSync } from "node:fs";
import { resolve } from "node:path";
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

describe("primary button contrast", () => {
  const css = readFileSync(resolve(__dirname, "../../app/globals.css"), "utf8");
  const luminance = (hex: string) => {
    const [r, g, b] = [1, 3, 5].map((i) => {
      const c = parseInt(hex.slice(i, i + 2), 16) / 255;
      return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
    });
    return 0.2126 * r + 0.7152 * g + 0.0722 * b;
  };
  const ratio = (a: string, b: string) => {
    const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
    return (hi + 0.05) / (lo + 0.05);
  };
  const mix = (a: string, b: string) =>
    "#" + [1, 3, 5].map((i) => ((parseInt(a.slice(i, i + 2), 16) + parseInt(b.slice(i, i + 2), 16)) >> 1).toString(16).padStart(2, "0")).join("");
  const tokens = (name: string) => [...css.matchAll(new RegExp(`--${name}: (#[0-9a-f]{6})`, "g"))].map((m) => m[1]);

  // CSS brightness() scales the whole element (text and background) by the same factor.
  const dim = (hex: string, k: number) =>
    "#" + [1, 3, 5].map((i) => Math.min(255, Math.round(parseInt(hex.slice(i, i + 2), 16) * k)).toString(16).padStart(2, "0")).join("");
  const brightness = (cls: string) => Number(/hover:brightness-(\d+)/.exec(cls)?.[1] ?? 100) / 100;
  const activeBrightness = Number(/active:brightness-(\d+)/.exec(readFileSync(resolve(__dirname, "Button.tsx"), "utf8"))?.[1]) / 100;

  it("uses the .9 hover and a milder active brightness", () => {
    expect(brightness(readFileSync(resolve(__dirname, "Button.tsx"), "utf8"))).toBe(0.9);
    expect(activeBrightness).toBeGreaterThanOrEqual(0.85);
  });

  it.each([0, 1])("keeps text >= 4.5:1 across the gradient at rest, hover and active (theme %i)", (i) => {
    const from = tokens("btn-from")[i];
    const to = tokens("btn-to")[i];
    const text = tokens("btn-text")[i];
    for (const k of [1, 0.9, activeBrightness])
      for (const bg of [from, mix(from, to), to]) expect(ratio(dim(text, k), dim(bg, k))).toBeGreaterThanOrEqual(4.5);
  });
});
