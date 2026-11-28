import { StrictMode } from "react";
import { configure, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Workspace } from "./Workspace";

const encoder = new TextEncoder();
const ev = (name: string, data: unknown) => `event: ${name}\ndata: ${JSON.stringify(data)}\n\n`;
const sse = (...events: string[]) =>
  new Response(
    new ReadableStream<Uint8Array>({
      start(c) {
        c.enqueue(encoder.encode(events.join("")));
        c.close();
      },
    }),
    { status: 200, headers: { "Content-Type": "text/event-stream" } },
  );
const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
const notFound = (code: string) => json(404, { error: { code, message: "nope", request_id: "r" } });

const doc = (doc_type: string) => ({
  doc_id: `id-${doc_type}`,
  doc_type,
  source: "upload",
  filename: doc_type === "resume" ? "cv.pdf" : null,
  status: "ready",
  chunk_count: 3,
  quarantined_count: 0,
  created_at: "2026-11-27T10:00:00Z",
});
const analysis = {
  analysis_id: "a1",
  score: 72,
  required_coverage: 0.75,
  nice_to_have_coverage: null,
  downgraded_count: 0,
  analyst: "fake",
  cached: true,
  request_id: "r",
  skills: [],
};
// Generous async timeout: these flows type a job description and stream several responses,
// which can exceed the 1 s default when the whole suite runs in parallel.
configure({ asyncUtilTimeout: 4000 });

const suggestions = { analysis_id: "a1", suggestions: [], rejected_count: 0, analyst: "fake", request_id: "r" };

describe("Workspace", { timeout: 15000 }, () => {
  const fetchMock = vi.fn();
  let documents: unknown[];
  let storedAnalysis: unknown;
  let storedSuggestions: unknown;
  beforeEach(() => {
    documents = [];
    storedAnalysis = null;
    storedSuggestions = null;
    fetchMock.mockReset().mockImplementation((path: string) => {
      if (path === "/api/documents") return Promise.resolve(json(200, { documents }));
      if (path === "/api/analyses/latest")
        return Promise.resolve(storedAnalysis ? json(200, storedAnalysis) : notFound("analysis_not_found"));
      if (path === "/api/suggestions/latest")
        return Promise.resolve(storedSuggestions ? json(200, storedSuggestions) : notFound("suggestions_not_found"));
      if (path === "/api/stream/documents") return Promise.resolve(sse(ev("done", { doc_type: "jd", quarantined_count: 0 })));
      if (path === "/api/stream/analyses") return Promise.resolve(sse(ev("done", { ...analysis, cached: false })));
      if (path === "/api/stream/suggestions") return Promise.resolve(sse(ev("done", suggestions)));
      return Promise.resolve(json(404, {}));
    });
    vi.stubGlobal("fetch", fetchMock);
  });
  afterEach(() => vi.unstubAllGlobals());

  const posts = (path: string) => fetchMock.mock.calls.filter((c) => c[0] === path).length;
  const stepButton = (name: RegExp) => within(screen.getByRole("navigation", { name: /progress/i })).getByRole("button", { name });
  const queryStepButton = (name: RegExp) =>
    within(screen.getByRole("navigation", { name: /progress/i })).queryByRole("button", { name });
  const stepTitle = (name: RegExp) => screen.findByRole("heading", { level: 1, name });

  async function replaceJd(user: ReturnType<typeof userEvent.setup>) {
    const replace = screen.queryByRole("button", { name: /replace job description/i });
    if (replace) await user.click(replace);
    await user.type(await screen.findByLabelText(/^job description$/i), "y".repeat(50));
    await user.click(screen.getByRole("button", { name: /add job description/i }));
  }

  it("lands on documents with later steps locked when nothing is stored", async () => {
    const user = userEvent.setup();
    render(<Workspace />);
    expect(await stepTitle(/step 1/i)).toBeInTheDocument();
    const match = stepButton(/match/i);
    expect(match).toBeDisabled();
    expect(screen.getByText(/add both documents first/i)).toBeVisible();
    await user.click(match);
    expect(screen.getByRole("heading", { level: 1, name: /step 1/i })).toBeInTheDocument();
  });

  it("restores to Match without running an analysis", async () => {
    documents = [doc("resume"), doc("jd")];
    render(<Workspace />);
    expect(await stepTitle(/step 2/i)).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: /^run analysis$/i })).toBeInTheDocument();
    expect(posts("/api/stream/analyses")).toBe(0);
  });

  it("on reload with an analysis but no suggestions, opens Improve without spending a suggestion", async () => {
    documents = [doc("resume"), doc("jd")];
    storedAnalysis = analysis;
    render(<Workspace />);
    expect(await stepTitle(/step 3/i)).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: /^suggest rewrites$/i })).toBeVisible();
    expect(posts("/api/stream/suggestions")).toBe(0);
    expect(posts("/api/stream/analyses")).toBe(0);
  });

  it("moves to Match on the first documents-ready and runs the analysis once", async () => {
    documents = [doc("resume")];
    const user = userEvent.setup();
    render(<Workspace />);
    await stepTitle(/step 1/i);
    // Wait for the stored resume to load so the later fetch cannot race the typing.
    await screen.findByText(/cv\.pdf/);
    documents = [doc("resume"), doc("jd")];
    await replaceJd(user);
    expect(await stepTitle(/step 2/i)).toBeInTheDocument();
    expect(await screen.findByText("72")).toBeInTheDocument();
    await waitFor(() => expect(stepButton(/match.*done/i)).toBeInTheDocument());
    expect(posts("/api/stream/analyses")).toBe(1);
  });

  it("runs suggestions when the user moves to Improve with none stored", async () => {
    documents = [doc("resume"), doc("jd")];
    storedAnalysis = analysis;
    const user = userEvent.setup();
    render(<Workspace />);
    await stepTitle(/step 3/i);
    await user.click(stepButton(/match/i));
    expect(await stepTitle(/step 2/i)).toBeInTheDocument();
    await user.click(await screen.findByRole("button", { name: /next: improve my resume/i }));
    expect(await stepTitle(/step 3/i)).toBeInTheDocument();
    await waitFor(() => expect(posts("/api/stream/suggestions")).toBe(1));
    expect(posts("/api/stream/analyses")).toBe(0);
  });

  it("replacing one document while revisiting Step 1 stays put, clears ticks and starts no analysis", async () => {
    documents = [doc("resume"), doc("jd")];
    storedAnalysis = analysis;
    storedSuggestions = suggestions;
    const user = userEvent.setup();
    render(<Workspace />);
    expect(await stepTitle(/step 4/i)).toBeInTheDocument();
    expect(stepButton(/match.*done/i)).toBeInTheDocument();
    await user.click(stepButton(/documents/i));
    expect(await stepTitle(/step 1/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /continue to your match/i })).toBeInTheDocument();

    storedAnalysis = null;
    storedSuggestions = null;
    await replaceJd(user);
    await waitFor(() => expect(posts("/api/stream/documents")).toBe(1));
    await waitFor(() => expect(queryStepButton(/match.*done/i)).not.toBeInTheDocument());
    expect(queryStepButton(/improve.*done/i)).not.toBeInTheDocument();
    expect(stepButton(/improve/i)).toBeDisabled();
    expect(screen.getByRole("heading", { level: 1, name: /step 1/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /continue to your match/i })).toBeInTheDocument();
    expect(posts("/api/stream/analyses")).toBe(0);

    await user.click(screen.getByRole("button", { name: /continue to your match/i }));
    expect(await stepTitle(/step 2/i)).toBeInTheDocument();
    await waitFor(() => expect(posts("/api/stream/analyses")).toBe(1));
  });

  it("opens Ask once an analysis exists without visiting Improve, keeping chat state across steps", async () => {
    documents = [doc("resume"), doc("jd")];
    storedAnalysis = analysis;
    const user = userEvent.setup();
    render(<Workspace />);
    await stepTitle(/step 3/i);
    const ask = stepButton(/ask/i);
    expect(ask).toBeEnabled();
    await user.click(ask);
    expect(await stepTitle(/step 4/i)).toBeInTheDocument();
    await user.type(screen.getByLabelText(/ask a question/i), "draft");
    await user.click(stepButton(/match/i));
    await user.click(stepButton(/ask/i));
    expect(screen.getByLabelText(/ask a question/i)).toHaveValue("draft");
  });
  it("keeps the chat history when switching between steps", async () => {
    documents = [doc("resume"), doc("jd")];
    storedAnalysis = analysis;
    const base = fetchMock.getMockImplementation()!;
    fetchMock.mockImplementation((path: string, init?: unknown) =>
      path === "/api/stream/chat"
        ? Promise.resolve(sse(ev("done", { answer: "A fine answer.", citations: [], invalid_citations: [], refused: false, guardrails: [], guardrail_checks: [], blocked: false, cached: false })))
        : base(path, init),
    );
    const user = userEvent.setup();
    render(<Workspace />);
    await stepTitle(/step 3/i);
    await user.click(stepButton(/ask/i));
    await user.click(await screen.findByRole("button", { name: "What's my biggest gap?" }));
    expect(await screen.findByText("A fine answer.", { selector: "p" })).toBeInTheDocument();
    await user.click(stepButton(/match/i));
    await user.click(stepButton(/ask/i));
    expect(screen.getByText("A fine answer.", { selector: "p" })).toBeVisible();
    expect(posts("/api/stream/chat")).toBe(1);
  });
  it.each([
    ["Match", false, /step 2/i, "/api/stream/analyses"],
    ["Improve", true, /step 3/i, "/api/stream/suggestions"],
  ])("clicking the current step (%s) after a reload starts no paid run", async (name, withAnalysis, title, path) => {
    documents = [doc("resume"), doc("jd")];
    storedAnalysis = withAnalysis ? analysis : null;
    const user = userEvent.setup();
    render(<Workspace />);
    await stepTitle(title);
    await user.click(stepButton(new RegExp(name, "i")));
    await user.click(stepButton(new RegExp(name, "i")));
    expect(screen.getByRole("heading", { level: 1, name: title })).toBeInTheDocument();
    await new Promise((r) => setTimeout(r, 50));
    expect(posts(path)).toBe(0);
    expect(posts("/api/stream/analyses")).toBe(0);
  });

  it("shows an error on Step 1 when the documents request fails", async () => {
    fetchMock.mockImplementation(() => Promise.resolve(json(500, {})));
    render(<Workspace />);
    expect(await stepTitle(/step 1/i)).toBeInTheDocument();
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(stepButton(/match/i)).toBeDisabled();
  });

  it("does not auto-run when the stored analysis cannot be read", async () => {
    documents = [doc("resume"), doc("jd")];
    const base = fetchMock.getMockImplementation()!;
    fetchMock.mockImplementation((path: string, init?: RequestInit) =>
      path === "/api/analyses/latest" ? Promise.resolve(json(500, {})) : base(path, init),
    );
    render(<Workspace />);
    expect(await stepTitle(/step 2/i)).toBeInTheDocument();
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(posts("/api/stream/analyses")).toBe(0);
  });

  it("falls back to Match when the analysis restore saw is not readable by the step", async () => {
    documents = [doc("resume"), doc("jd")];
    storedAnalysis = analysis;
    const base = fetchMock.getMockImplementation()!;
    let reads = 0;
    fetchMock.mockImplementation((path: string, init?: RequestInit) =>
      path === "/api/analyses/latest" && ++reads > 1 ? Promise.resolve(json(500, {})) : base(path, init),
    );
    render(<Workspace />);
    expect(await stepTitle(/step 2/i)).toBeInTheDocument();
    expect(stepButton(/improve/i)).toBeDisabled();
    expect(posts("/api/stream/analyses")).toBe(0);
    expect(posts("/api/stream/suggestions")).toBe(0);
  });

  it("under StrictMode, the first documents-ready starts exactly one analysis", async () => {
    documents = [doc("resume")];
    const user = userEvent.setup();
    render(
      <StrictMode>
        <Workspace />
      </StrictMode>,
    );
    await stepTitle(/step 1/i);
    // Wait for the stored resume to load so the later fetch cannot race the typing.
    await screen.findByText(/cv\.pdf/);
    documents = [doc("resume"), doc("jd")];
    await replaceJd(user);
    expect(await stepTitle(/step 2/i)).toBeInTheDocument();
    expect(await screen.findByText("72")).toBeInTheDocument();
    await new Promise((r) => setTimeout(r, 50));
    expect(posts("/api/stream/analyses")).toBe(1);
  });
});
