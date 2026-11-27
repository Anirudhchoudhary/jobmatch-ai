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
  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
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
    const btn = await screen.findByRole("button", { name: /r1/ });
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
    await screen.findByText("First answer");
    await ask(user, "Q2");
    await screen.findByText("Second answer");
    const items = screen.getAllByRole("listitem", { name: /conversation turn/i });
    expect(items[0]).toHaveTextContent("Q1");
    expect(items[1]).toHaveTextContent("Q2");
  });
});
