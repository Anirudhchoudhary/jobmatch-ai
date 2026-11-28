import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Suggestions } from "./Suggestions";

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

const sse = (blocks: string[]) =>
  new Response(blocks.join(""), { status: 200, headers: { "Content-Type": "text/event-stream" } });
const ev = (name: string, data: unknown) => `event: ${name}\ndata: ${JSON.stringify(data)}\n\n`;

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
    post = () => sse([ev("progress", { step: "loading" }), ev("progress", { step: "rewriting" }), ev("done", stored())]);
    fetchMock.mockReset().mockImplementation((path: string) => {
      if (path === "/api/suggestions/latest") return Promise.resolve(latest());
      if (path === "/api/stream/suggestions") return Promise.resolve(post());
      return Promise.resolve(json(404, {}));
    });
    vi.stubGlobal("fetch", fetchMock);
  });
  afterEach(() => vi.unstubAllGlobals());

  const calledPost = () => fetchMock.mock.calls.some((c) => c[0] === "/api/stream/suggestions");

  const posts = () => fetchMock.mock.calls.filter((c) => c[0] === "/api/stream/suggestions").length;

  it("autoRun starts exactly one run when none are stored", async () => {
    const onResult = vi.fn();
    const { rerender } = render(<Suggestions autoRun onResult={onResult} />);
    expect(await screen.findByText("Wrote backend code")).toBeInTheDocument();
    rerender(<Suggestions autoRun onResult={onResult} />);
    expect(posts()).toBe(1);
    await waitFor(() => expect(onResult).toHaveBeenLastCalledWith(true));
  });

  it("autoRun does not run when rewrites are stored, and without autoRun nothing runs", async () => {
    latest = () => json(200, stored());
    const { unmount } = render(<Suggestions autoRun />);
    await screen.findByText("Wrote backend code");
    unmount();
    latest = () => json(404, { error: { code: "suggestions_not_found", message: "x", request_id: "r" } });
    const onResult = vi.fn();
    render(<Suggestions onResult={onResult} />);
    expect(await screen.findByRole("button", { name: /^suggest rewrites$/i })).toBeInTheDocument();
    await waitFor(() => expect(onResult).toHaveBeenLastCalledWith(false));
    expect(posts()).toBe(0);
  });

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

  it("streams from /api/stream/suggestions when Suggest rewrites is clicked on a 404", async () => {
    const user = userEvent.setup();
    render(<Suggestions />);
    await user.click(await screen.findByRole("button", { name: /suggest rewrites/i }));
    expect(await screen.findByText("Wrote backend code")).toBeInTheDocument();
    expect(calledPost()).toBe(true);
    expect(fetchMock.mock.calls.some((c) => c[0] === "/api/suggestions")).toBe(false);
    const [, init] = fetchMock.mock.calls.find((c) => c[0] === "/api/stream/suggestions")!;
    expect(init.method).toBe("POST");
    expect(new Headers(init.headers).get("Content-Type")).toBe("application/json");
  });

  it("shows the current step while the stream runs", async () => {
    let release: (() => void) | undefined;
    post = () =>
      new Response(
        new ReadableStream<Uint8Array>({
          start(controller) {
            const enc = new TextEncoder();
            controller.enqueue(enc.encode(ev("progress", { step: "rewriting" })));
            release = () => {
              controller.enqueue(enc.encode(ev("done", stored())));
              controller.close();
            };
          },
        }),
        { status: 200, headers: { "Content-Type": "text/event-stream" } },
      );
    const user = userEvent.setup();
    render(<Suggestions />);
    await user.click(await screen.findByRole("button", { name: /suggest rewrites/i }));
    expect(await screen.findByRole("status")).toHaveTextContent(/rewriting/i);
    release?.();
    expect(await screen.findByText("Wrote backend code")).toBeInTheDocument();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("shows the message of an error event", async () => {
    post = () => sse([ev("error", { code: "analysis_required", message: "Run a match analysis first.", request_id: "r" })]);
    const user = userEvent.setup();
    render(<Suggestions />);
    await user.click(await screen.findByRole("button", { name: /suggest rewrites/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Run a match analysis first.");
  });

  it("shows the server message when the request fails", async () => {
    post = () => json(429, { error: { code: "allowance_exceeded", message: "Daily limit reached.", request_id: "r" } });
    const user = userEvent.setup();
    render(<Suggestions />);
    await user.click(await screen.findByRole("button", { name: /suggest rewrites/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Daily limit reached.");
  });
});
