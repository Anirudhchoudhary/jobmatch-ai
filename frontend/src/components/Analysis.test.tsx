import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Analysis } from "./Analysis";

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

const req = (skill: string, importance: string) => ({ skill, text: `${skill} text`, importance, jd_chunk_id: "jd1" });
const analysis = (over: Record<string, unknown> = {}) => ({
  analysis_id: "a1",
  score: 72,
  required_coverage: 0.75,
  nice_to_have_coverage: 0.5,
  downgraded_count: 0,
  analyst: "fake",
  cached: true,
  request_id: "r",
  skills: [
    { requirement: req("Python", "required"), status: "matched", evidence: [{ chunk_id: "c1", quote: "built APIs in python" }] },
    { requirement: req("Go", "required"), status: "partial", evidence: [] },
    { requirement: req("Rust", "nice_to_have"), status: "missing", evidence: [] },
    { requirement: req("SQL", "required"), status: "missing", evidence: [] },
  ],
  ...over,
});

describe("Analysis", () => {
  const fetchMock = vi.fn();
  let latest: () => Response;
  let post: () => Response;
  beforeEach(() => {
    latest = () => notFound("analysis_not_found");
    post = () => json(500, {});
    fetchMock.mockReset().mockImplementation((path: string) => {
      if (path === "/api/analyses/latest") return Promise.resolve(latest());
      if (path === "/api/stream/analyses") return Promise.resolve(post());
      if (path === "/api/suggestions/latest") return Promise.resolve(notFound("suggestions_not_found"));
      if (path === "/api/chunks/c1")
        return Promise.resolve(json(200, { chunk_id: "c1", doc_type: "resume", section: "Experience", text: "At Acme I Built APIs in Python for years." }));
      if (path === "/api/chunks/gone") return Promise.resolve(notFound("chunk_not_found"));
      return Promise.resolve(json(404, {}));
    });
    vi.stubGlobal("fetch", fetchMock);
  });
  afterEach(() => vi.unstubAllGlobals());

  const calledPost = () => fetchMock.mock.calls.some((c) => c[0] === "/api/stream/analyses");

  const posts = () => fetchMock.mock.calls.filter((c) => c[0] === "/api/stream/analyses").length;

  it("autoRun starts exactly one run on a 404 and reports the result", async () => {
    post = () => sse(ev("done", analysis({ cached: false })));
    const onResult = vi.fn();
    const { rerender } = render(<Analysis docsVersion={1} autoRun onResult={onResult} />);
    expect(await screen.findByText("72")).toBeInTheDocument();
    rerender(<Analysis docsVersion={1} autoRun onResult={onResult} />);
    expect(posts()).toBe(1);
    await waitFor(() => expect(onResult).toHaveBeenLastCalledWith("a1"));
  });

  it("autoRun does not re-run after a failed run, nor with a stored result", async () => {
    post = () => sse(ev("error", { code: "allowance_exceeded", message: "Daily limit reached." }));
    const { rerender, unmount } = render(<Analysis docsVersion={1} autoRun />);
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("Daily limit reached."));
    rerender(<Analysis docsVersion={1} autoRun />);
    expect(posts()).toBe(1);
    unmount();
    latest = () => json(200, analysis());
    render(<Analysis docsVersion={1} autoRun />);
    await screen.findByText("72");
    expect(posts()).toBe(1);
  });

  it("without autoRun a 404 shows the run button and reports no result", async () => {
    const onResult = vi.fn();
    render(<Analysis docsVersion={1} onResult={onResult} />);
    expect(await screen.findByRole("button", { name: /^run analysis$/i })).toBeInTheDocument();
    await waitFor(() => expect(onResult).toHaveBeenLastCalledWith(null));
    expect(posts()).toBe(0);
  });

  it("restores the stored analysis without calling the POST", async () => {
    latest = () => json(200, analysis());
    render(<Analysis docsVersion={1} />);
    expect(await screen.findByText("72")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^run analysis$/i })).not.toBeInTheDocument();
    expect(calledPost()).toBe(false);
  });

  it("shows the run button on 404 and runs the stream, then shows the result", async () => {
    post = () =>
      sse(ev("progress", { step: "loading" }), ev("progress", { step: "matching" }), ev("done", analysis({ cached: false })));
    const user = userEvent.setup();
    render(<Analysis docsVersion={1} />);
    await user.click(await screen.findByRole("button", { name: /^run analysis$/i }));
    expect(await screen.findByText("72")).toBeInTheDocument();
    expect(calledPost()).toBe(true);
  });

  it("shows required and nice-to-have coverage as percentages", async () => {
    latest = () => json(200, analysis());
    render(<Analysis docsVersion={1} />);
    expect((await screen.findByText(/required coverage/i)).closest("p")).toHaveTextContent("75%");
    expect(screen.getByText(/nice-to-have coverage/i).closest("p")).toHaveTextContent("50%");
  });

  it("groups skills by status with counts and importance", async () => {
    latest = () => json(200, analysis());
    render(<Analysis docsVersion={1} />);
    const matched = await screen.findByRole("region", { name: /matched \(1\)/i });
    expect(within(matched).getByText("Python")).toBeInTheDocument();
    expect(within(matched).getByText(/required/i)).toBeInTheDocument();
    expect(screen.getByRole("region", { name: /partial \(1\)/i })).toHaveTextContent("Go");
    const missing = screen.getByRole("region", { name: /missing \(2\)/i });
    expect(missing).toHaveTextContent("Rust");
    expect(missing).toHaveTextContent(/nice to have/i);
  });

  it("fetches the chunk when evidence is clicked and highlights the quote case-insensitively", async () => {
    latest = () => json(200, analysis());
    const user = userEvent.setup();
    render(<Analysis docsVersion={1} />);
    await user.click(await screen.findByRole("button", { name: /built apis in python/i }));
    const mark = await screen.findByText("Built APIs in Python", { selector: "mark" });
    expect(mark).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith("/api/chunks/c1", expect.anything());
    expect(screen.getByText(/at acme i/i)).toBeInTheDocument();
  });

  it("shows the quote above the chunk when it is not found in the text", async () => {
    const a = analysis();
    a.skills[0].evidence = [{ chunk_id: "c1", quote: "totally different words" }];
    latest = () => json(200, a);
    const user = userEvent.setup();
    render(<Analysis docsVersion={1} />);
    await user.click(await screen.findByRole("button", { name: /totally different words/i }));
    expect(await screen.findByText(/at acme i built apis/i)).toBeInTheDocument();
    expect(document.querySelector("mark")).toBeNull();
    expect(screen.getByText(/quote not found in this section/i)).toBeInTheDocument();
  });

  it("shows an error when the chunk cannot be loaded", async () => {
    const a = analysis();
    a.skills[0].evidence = [{ chunk_id: "gone", quote: "q" }];
    latest = () => json(200, a);
    const user = userEvent.setup();
    render(<Analysis docsVersion={1} />);
    await user.click(await screen.findByRole("button", { name: "q" }));
    expect(await screen.findByRole("alert")).toBeInTheDocument();
  });

  it("notes downgraded evidence only when the count is above zero", async () => {
    latest = () => json(200, analysis({ downgraded_count: 3 }));
    const { unmount } = render(<Analysis docsVersion={1} />);
    expect(await screen.findByText("3 evidence quotes were removed because they didn't match the resume")).toBeInTheDocument();
    unmount();
    latest = () => json(200, analysis());
    render(<Analysis docsVersion={1} />);
    await screen.findByText("72");
    expect(screen.queryByText(/were removed/i)).not.toBeInTheDocument();
  });

  it("offers a re-run instead of auto-running when documents change", async () => {
    latest = () => json(200, analysis());
    const { rerender } = render(<Analysis docsVersion={1} />);
    await screen.findByText("72");
    rerender(<Analysis docsVersion={2} />);
    expect(await screen.findByRole("button", { name: /re-run analysis/i })).toBeInTheDocument();
    expect(calledPost()).toBe(false);
  });

  it("shows an error when the analysis stream fails", async () => {
    post = () => sse(ev("error", { code: "allowance_exceeded", message: "Daily limit reached." }));
    const user = userEvent.setup();
    render(<Analysis docsVersion={1} />);
    await user.click(await screen.findByRole("button", { name: /^run analysis$/i }));
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("Daily limit reached."));
    expect(screen.getByRole("button", { name: /^run analysis$/i })).toBeEnabled();
  });

  it("keeps a run that finishes after a document change stale", async () => {
    let finish!: () => void;
    post = () =>
      new Response(
        new ReadableStream<Uint8Array>({
          start(c) {
            finish = () => {
              c.enqueue(encoder.encode(ev("done", analysis({ score: 41, cached: false }))));
              c.close();
            };
          },
        }),
        { status: 200, headers: { "Content-Type": "text/event-stream" } },
      );
    const user = userEvent.setup();
    const { rerender } = render(<Analysis docsVersion={1} />);
    await user.click(await screen.findByRole("button", { name: /^run analysis$/i }));
    await waitFor(() => expect(calledPost()).toBe(true));
    rerender(<Analysis docsVersion={2} />);
    finish();
    await waitFor(() => expect(screen.getByRole("button", { name: /^run analysis$/i })).toBeEnabled());
    expect(screen.queryByText("41")).not.toBeInTheDocument();
    expect(screen.getByText(/documents changed/i)).toBeInTheDocument();
  });

  it("exposes the score as a meter with value, min and max", async () => {
    latest = () => json(200, analysis());
    render(<Analysis docsVersion={1} />);
    const meter = await screen.findByRole("meter", { name: /match score/i });
    expect(meter).toHaveAttribute("aria-valuenow", "72");
    expect(meter).toHaveAttribute("aria-valuemin", "0");
    expect(meter).toHaveAttribute("aria-valuemax", "100");
  });

  it("Next calls goTo('improve') and only shows with a result", async () => {
    const goTo = vi.fn();
    const user = userEvent.setup();
    const { unmount } = render(<Analysis docsVersion={1} goTo={goTo} />);
    await screen.findByRole("button", { name: /^run analysis$/i });
    expect(screen.queryByRole("button", { name: /next: improve my resume/i })).not.toBeInTheDocument();
    unmount();
    latest = () => json(200, analysis());
    render(<Analysis docsVersion={1} goTo={goTo} />);
    await user.click(await screen.findByRole("button", { name: /next: improve my resume/i }));
    expect(goTo).toHaveBeenCalledWith("improve");
  });

  it("shows friendly progress labels while running", async () => {
    let release!: () => void;
    post = () =>
      new Response(
        new ReadableStream<Uint8Array>({
          start(c) {
            c.enqueue(encoder.encode(ev("progress", { step: "matching" })));
            release = () => {
              c.enqueue(encoder.encode(ev("done", analysis())));
              c.close();
            };
          },
        }),
        { status: 200, headers: { "Content-Type": "text/event-stream" } },
      );
    render(<Analysis docsVersion={1} autoRun />);
    const status = await screen.findByText("Matching skills with evidence");
    const line = status.closest("[role=status]")!;
    expect(line).toHaveTextContent("Reading the job");
    expect(line).toHaveTextContent("Reading the resume");
    release();
    await screen.findByText("72");
  });

  it("never reports null while re-running over a stored result", async () => {
    latest = () => json(200, analysis());
    post = () => sse(ev("done", analysis({ score: 90, cached: false })));
    const onResult = vi.fn();
    const user = userEvent.setup();
    render(<Analysis docsVersion={1} onResult={onResult} />);
    await user.click((await screen.findAllByRole("button", { name: /re-run analysis/i }))[0]);
    await screen.findByText("90");
    expect(onResult).not.toHaveBeenCalledWith(null);
    expect(posts()).toBe(1);
  });

  it("keeps the result reported when a re-run fails", async () => {
    latest = () => json(200, analysis());
    post = () => sse(ev("error", { code: "allowance_exceeded", message: "Daily limit reached." }));
    const onResult = vi.fn();
    const user = userEvent.setup();
    render(<Analysis docsVersion={1} onResult={onResult} />);
    await user.click((await screen.findAllByRole("button", { name: /re-run analysis/i }))[0]);
    await screen.findByRole("alert");
    expect(onResult).not.toHaveBeenCalledWith(null);
    expect(screen.getByText("72")).toBeInTheDocument();
  });

  it("highlights quotes containing regex characters", async () => {
    const a = analysis();
    a.skills[0].evidence = [{ chunk_id: "c1", quote: "built apis (in python)?" }];
    latest = () => json(200, a);
    const user = userEvent.setup();
    render(<Analysis docsVersion={1} />);
    await user.click(await screen.findByRole("button", { name: /built apis \(in python\)\?/i }));
    await screen.findByText(/at acme i/i);
    expect(document.querySelector("mark")).toBeNull();
  });
});
