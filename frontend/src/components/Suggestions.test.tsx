import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Suggestions } from "./Suggestions";

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

const stored = (rejected_count = 0) => ({
  analysis_id: "a1",
  rejected_count,
  analyst: "fake",
  request_id: "r",
  suggestions: [
    {
      original_chunk_id: "c1",
      original: "Wrote backend code",
      rewritten: "Built Python APIs serving 1M requests",
      rationale: "Names the skill and scale.",
      target_skills: ["Python", "APIs"],
    },
  ],
});

describe("Suggestions", () => {
  const fetchMock = vi.fn();
  let latest: () => Response;
  let post: () => Response;
  beforeEach(() => {
    latest = () => json(404, { error: { code: "suggestions_not_found", message: "No suggestions yet.", request_id: "r" } });
    post = () => json(200, stored());
    fetchMock.mockReset().mockImplementation((path: string) => {
      if (path === "/api/suggestions/latest") return Promise.resolve(latest());
      if (path === "/api/suggestions") return Promise.resolve(post());
      return Promise.resolve(json(404, {}));
    });
    vi.stubGlobal("fetch", fetchMock);
  });
  afterEach(() => vi.unstubAllGlobals());

  const calledPost = () => fetchMock.mock.calls.some((c) => c[0] === "/api/suggestions");

  it("restores stored suggestions as original and rewritten pairs without posting", async () => {
    latest = () => json(200, stored());
    render(<Suggestions />);
    expect(await screen.findByText("Wrote backend code")).toBeInTheDocument();
    expect(screen.getByText("Built Python APIs serving 1M requests")).toBeInTheDocument();
    expect(screen.getByText("Names the skill and scale.")).toBeInTheDocument();
    expect(screen.getByText("Python")).toBeInTheDocument();
    expect(screen.getByText("APIs")).toBeInTheDocument();
    expect(calledPost()).toBe(false);
  });

  it("shows the rejected count only when above zero", async () => {
    latest = () => json(200, stored(2));
    const { unmount } = render(<Suggestions />);
    expect(await screen.findByText(/2 suggestions were rejected/i)).toBeInTheDocument();
    unmount();
    latest = () => json(200, stored());
    render(<Suggestions />);
    await screen.findByText("Wrote backend code");
    expect(screen.queryByText(/were rejected/i)).not.toBeInTheDocument();
  });

  it("posts when Suggest rewrites is clicked on a 404", async () => {
    const user = userEvent.setup();
    render(<Suggestions />);
    await user.click(await screen.findByRole("button", { name: /suggest rewrites/i }));
    expect(await screen.findByText("Wrote backend code")).toBeInTheDocument();
    expect(calledPost()).toBe(true);
  });

  it("shows the server message when the request fails", async () => {
    post = () => json(429, { error: { code: "allowance_exceeded", message: "Daily limit reached.", request_id: "r" } });
    const user = userEvent.setup();
    render(<Suggestions />);
    await user.click(await screen.findByRole("button", { name: /suggest rewrites/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Daily limit reached.");
  });
});
