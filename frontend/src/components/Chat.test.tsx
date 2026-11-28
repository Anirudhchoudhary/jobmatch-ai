import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Chat } from "./Chat";

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

const allowVerdict = { grounded: true, action: "allow", unsupported_claims: [], mode: "enforce" };
const chk = (guardrail: string, action = "allow", stage = "chat_input") => ({
  guardrail,
  provider: "local",
  stage,
  action,
  mode: "enforce",
  reason: "r",
  latency_ms: 1,
  items: [],
});
const done = (over: Record<string, unknown> = {}) => ({
  answer: "Python at Acme [r1].",
  citations: [{ label: "r1", chunk_id: "c1", doc_type: "resume", section: "Experience", text: "Built Python APIs at Acme" }],
  invalid_citations: [],
  refused: false,
  prompt_version: "v1",
  request_id: "req",
  guardrails: [],
  guardrail_checks: [chk("topic"), chk("groundedness", "allow", "output")],
  blocked: false,
  cached: false,
  ...over,
});

describe("Chat", () => {
  const fetchMock = vi.fn();
  const reduced = (on: boolean) => vi.stubGlobal("matchMedia", (q: string) => ({ matches: on && q.includes("reduce") }));
  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
    reduced(true);
  });
  afterEach(() => vi.unstubAllGlobals());

  const ask = async (user: ReturnType<typeof userEvent.setup>, q = "What do I know?") => {
    await user.type(screen.getByLabelText(/ask a question/i), q);
    await user.click(screen.getByRole("button", { name: "Send" }));
  };

  it("posts the question and shows the answer with a clickable citation", async () => {
    fetchMock.mockResolvedValue(
      sse(
        ev("progress", { step: "retrieving" }),
        ev("token", { text: "Python at " }),
        ev("token", { text: "Acme [r1]." }),
        ev("verdict", allowVerdict),
        ev("done", done()),
      ),
    );
    const user = userEvent.setup();
    render(<Chat />);
    await ask(user);
    expect(fetchMock.mock.calls[0][0]).toBe("/api/stream/chat");
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({ question: "What do I know?" });
    const btn = await screen.findByRole("button", { name: /Source 1/ });
    await user.click(btn);
    expect(screen.getByText("Built Python APIs at Acme")).toBeInTheDocument();
    expect(screen.getByText("What do I know?")).toBeInTheDocument();
    expect(screen.getByLabelText(/ask a question/i)).toHaveValue("");
  });

  it("renders tokens incrementally and disables send while streaming", async () => {
    let ctrl!: ReadableStreamDefaultController<Uint8Array>;
    fetchMock.mockResolvedValue(
      new Response(new ReadableStream<Uint8Array>({ start: (c) => (ctrl = c) }), { status: 200 }),
    );
    const user = userEvent.setup();
    render(<Chat />);
    await ask(user);
    await waitFor(() => expect(ctrl).toBeDefined());
    ctrl.enqueue(encoder.encode(ev("token", { text: "Hello " })));
    expect(await screen.findByText(/Hello/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /sending|send/i })).toBeDisabled();
    ctrl.enqueue(encoder.encode(ev("token", { text: "world" })));
    expect(await screen.findByText(/Hello world/)).toBeInTheDocument();
    ctrl.enqueue(encoder.encode(ev("verdict", allowVerdict) + ev("done", done({ answer: "Hello world" }))));
    ctrl.close();
    await waitFor(() => expect(screen.getByLabelText(/ask a question/i)).toBeEnabled());
  });

  it("shows a banner listing unsupported claims for a flagged verdict", async () => {
    fetchMock.mockResolvedValue(
      sse(
        ev("token", { text: "Led 50 people." }),
        ev("verdict", { grounded: false, action: "flag", unsupported_claims: ["Led 50 people."], mode: "enforce" }),
        ev("done", done({ answer: "Led 50 people.", citations: [] })),
      ),
    );
    const user = userEvent.setup();
    render(<Chat />);
    await ask(user);
    const banner = await screen.findByRole("alert");
    expect(banner).toHaveTextContent(/parts of this answer could not be verified/i);
    expect(within(banner).getByText("Led 50 people.")).toBeInTheDocument();
  });

  it("marks blocked and cached answers", async () => {
    fetchMock
      .mockResolvedValueOnce(
        sse(
          ev("verdict", allowVerdict),
          ev("done", done({ answer: "I can only help with questions about your resume and this job description.", citations: [], blocked: true, refused: true, guardrail_checks: [chk("topic", "block")] })),
        ),
      )
      .mockResolvedValueOnce(sse(ev("verdict", allowVerdict), ev("done", done({ cached: true }))));
    const user = userEvent.setup();
    render(<Chat />);
    await ask(user, "weather?");
    expect(await screen.findByText("Blocked")).toBeInTheDocument();
    await ask(user, "again");
    expect(await screen.findByText(/served from cache/i)).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "Show guardrails" })).toHaveLength(2);
  });

  it("marks a refusal that was not blocked", async () => {
    fetchMock.mockResolvedValue(sse(ev("verdict", allowVerdict), ev("done", done({ answer: "I couldn't find that.", citations: [], refused: true }))));
    const user = userEvent.setup();
    render(<Chat />);
    await ask(user);
    expect(await screen.findByText("Not answered")).toBeInTheDocument();
  });

  it("shows stream_interrupted inline and re-enables input", async () => {
    fetchMock.mockResolvedValue(sse(ev("token", { text: "partial" })));
    const user = userEvent.setup();
    render(<Chat />);
    await ask(user);
    expect(await screen.findByRole("alert")).toHaveTextContent(/connection dropped/i);
    expect(screen.getByLabelText(/ask a question/i)).toBeEnabled();
  });

  it("shows server error messages (429) and re-enables input", async () => {
    fetchMock.mockResolvedValue(json(429, { error: { code: "allowance_exceeded", message: "Daily chat limit reached.", request_id: "r" } }));
    const user = userEvent.setup();
    render(<Chat />);
    await ask(user);
    expect(await screen.findByRole("alert")).toHaveTextContent("Daily chat limit reached.");
    expect(screen.getByLabelText(/ask a question/i)).toBeEnabled();
  });

  it("limits the question to 2000 characters and ignores blank input", async () => {
    const user = userEvent.setup();
    render(<Chat />);
    expect(screen.getByLabelText(/ask a question/i)).toHaveAttribute("maxlength", "2000");
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
    await user.type(screen.getByLabelText(/ask a question/i), "   ");
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("keeps past answers, newest last", async () => {
    fetchMock
      .mockResolvedValueOnce(sse(ev("verdict", allowVerdict), ev("done", done({ answer: "First answer", citations: [] }))))
      .mockResolvedValueOnce(sse(ev("verdict", allowVerdict), ev("done", done({ answer: "Second answer", citations: [] }))));
    const user = userEvent.setup();
    render(<Chat />);
    await ask(user, "Q1");
    await screen.findByText("First answer", { selector: "p" });
    await ask(user, "Q2");
    await screen.findByText("Second answer", { selector: "p" });
    const items = screen.getAllByRole("listitem", { name: /conversation turn/i });
    expect(items[0]).toHaveTextContent("Q1");
    expect(items[1]).toHaveTextContent("Q2");
  });

  it("shows the working line step by step and ends with the source count", async () => {
    let ctrl!: ReadableStreamDefaultController<Uint8Array>;
    fetchMock.mockResolvedValue(
      new Response(new ReadableStream<Uint8Array>({ start: (c) => (ctrl = c) }), { status: 200 }),
    );
    const user = userEvent.setup();
    render(<Chat />);
    await ask(user);
    await waitFor(() => expect(ctrl).toBeDefined());
    const push = (name: string, data: unknown) => ctrl.enqueue(encoder.encode(ev(name, data)));
    push("progress", { step: "checking_input" });
    expect(await screen.findByText("Checking your question")).toBeInTheDocument();
    push("progress", { step: "retrieving" });
    expect(await screen.findByText("Checked your question")).toBeInTheDocument();
    expect(screen.getByText("Searching your documents")).toBeInTheDocument();
    push("progress", { step: "reranking" });
    push("progress", { step: "generating" });
    expect(await screen.findByText("Searched your documents")).toBeInTheDocument();
    expect(screen.getByText("Verifying against sources")).toBeInTheDocument();
    const two = [
      { label: "r1", chunk_id: "c1", doc_type: "resume", section: "Summary", text: "a" },
      { label: "j1", chunk_id: "c2", doc_type: "jd", section: "Requirements", text: "b" },
    ];
    push("verdict", allowVerdict);
    push("done", done({ answer: "Kafka [r1] and Go [j1] and again [r1].", citations: two }));
    ctrl.close();
    expect(await screen.findByText("Verified against sources")).toBeInTheDocument();
    expect(screen.getByText(/2 sources cited/)).toBeInTheDocument();
  });

  it("keeps revealed text, stops the cursor and shows the error when the stream breaks", async () => {
    reduced(true);
    fetchMock.mockResolvedValue(sse(ev("token", { text: "partial answer" })));
    const user = userEvent.setup();
    const { container } = render(<Chat />);
    await ask(user);
    expect(await screen.findByRole("alert")).toHaveTextContent(/connection dropped/i);
    expect(screen.getByText("partial answer", { selector: "p" })).toBeInTheDocument();
    expect(container.querySelector("[data-cursor]")).toBeNull();
  });

  it("types the answer out gradually and shows a cursor", async () => {
    reduced(false);
    let ctrl!: ReadableStreamDefaultController<Uint8Array>;
    fetchMock.mockResolvedValue(
      new Response(new ReadableStream<Uint8Array>({ start: (c) => (ctrl = c) }), { status: 200 }),
    );
    const user = userEvent.setup();
    const { container } = render(<Chat />);
    await ask(user);
    await waitFor(() => expect(ctrl).toBeDefined());
    ctrl.enqueue(encoder.encode(ev("token", { text: "x".repeat(300) })));
    await waitFor(() => expect(container.querySelector("[data-cursor]")).not.toBeNull());
    const partial = container.querySelector("li p.whitespace-pre-wrap")!.textContent!.length;
    expect(partial).toBeLessThan(300);
    await waitFor(() => expect(container.querySelector("li p.whitespace-pre-wrap")!.textContent).toHaveLength(300));
  });

  it("copies the answer without label codes", async () => {
    fetchMock.mockResolvedValue(sse(ev("verdict", allowVerdict), ev("done", done({ answer: "A [r1][r1] B." }))));
    const user = userEvent.setup();
    const { container } = render(<Chat />);
    await ask(user);
    await screen.findAllByRole("button", { name: /Source 1/ });
    expect(container.querySelector("li p.whitespace-pre-wrap")!.textContent).not.toMatch(/r1/);
  });

  it("sends a starter question immediately when its chip is clicked", async () => {
    fetchMock.mockResolvedValueOnce(sse(ev("done", done())));
    const user = userEvent.setup();
    render(<Chat />);
    await user.click(screen.getByRole("button", { name: "What's my biggest gap?" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({ question: "What's my biggest gap?" });
    expect(await screen.findByText("What's my biggest gap?", { selector: "p" })).toBeInTheDocument();
  });

  it("offers all three starter questions and hides them once a conversation starts", async () => {
    fetchMock.mockResolvedValueOnce(sse(ev("done", done())));
    const user = userEvent.setup();
    render(<Chat />);
    for (const q of ["What's my biggest gap?", "Do I have the required experience?", "Which skills should I highlight?"])
      expect(screen.getByRole("button", { name: q })).toBeEnabled();
    await ask(user);
    await screen.findByRole("listitem", { name: "Conversation turn" });
    expect(screen.queryByRole("button", { name: "What's my biggest gap?" })).not.toBeInTheDocument();
  });

  it("ticks 'Verified against sources' only for a grounded answer", async () => {
    fetchMock.mockResolvedValueOnce(sse(ev("progress", { step: "generating" }), ev("verdict", allowVerdict), ev("done", done())));
    const user = userEvent.setup();
    render(<Chat />);
    await ask(user);
    expect(await screen.findByText("Verified against sources")).toBeInTheDocument();
  });

  it("shows a warning instead of 'Verified' for a flagged answer", async () => {
    const flag = { grounded: false, action: "flag", unsupported_claims: ["x"], mode: "enforce" };
    fetchMock.mockResolvedValueOnce(sse(ev("progress", { step: "generating" }), ev("verdict", flag), ev("done", done())));
    const user = userEvent.setup();
    render(<Chat />);
    await ask(user);
    expect(await screen.findByText("Some claims not verified")).toBeInTheDocument();
    expect(screen.queryByText("Verified against sources")).not.toBeInTheDocument();
  });

  it("does not tick steps never reached for a refused answer", async () => {
    fetchMock.mockResolvedValueOnce(
      sse(ev("progress", { step: "checking_input" }), ev("done", done({ refused: true, answer: "No.", citations: [] }))),
    );
    const user = userEvent.setup();
    render(<Chat />);
    await ask(user);
    await screen.findByText("Not answered");
    expect(screen.queryByText("Verified against sources")).not.toBeInTheDocument();
    expect(screen.queryByText("Searched your documents")).not.toBeInTheDocument();
    expect(screen.getByText("Checked your question")).toBeInTheDocument();
  });

  it("keeps a half-typed draft when a starter chip is sent", async () => {
    fetchMock.mockResolvedValueOnce(sse(ev("done", done())));
    const user = userEvent.setup();
    render(<Chat />);
    await user.type(screen.getByLabelText(/ask a question/i), "my draft");
    await user.click(screen.getByRole("button", { name: "What's my biggest gap?" }));
    await screen.findByRole("listitem", { name: "Conversation turn" });
    expect(screen.getByLabelText(/ask a question/i)).toHaveValue("my draft");
  });

  it("announces the final answer once in a polite live region", async () => {
    fetchMock.mockResolvedValueOnce(sse(ev("token", { text: "Python at Acme [r1]." }), ev("done", done())));
    const user = userEvent.setup();
    render(<Chat />);
    await ask(user);
    const live = await screen.findByTestId("answer-announce");
    await waitFor(() => expect(live).toHaveTextContent("Python at Acme ."));
    expect(live).toHaveAttribute("aria-live", "polite");
    expect(live).not.toHaveTextContent("[r1]");
  });

  it("strips citation codes from unsupported claims in the flagged alert", async () => {
    const flag = { grounded: false, action: "flag", unsupported_claims: ["Knows Kafka [r1] well [j2] ."], mode: "enforce" };
    fetchMock.mockResolvedValueOnce(sse(ev("verdict", flag), ev("done", done())));
    const user = userEvent.setup();
    render(<Chat />);
    await ask(user);
    expect(await screen.findByText("Knows Kafka well.")).toBeInTheDocument();
    expect(screen.queryByText(/\[[rj]\d+\]/)).not.toBeInTheDocument();
  });
});
