import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Documents } from "./Documents";

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

const doc = (doc_type: string, filename: string | null) => ({
  doc_id: `id-${doc_type}`,
  doc_type,
  source: "upload",
  filename,
  status: "ready",
  chunk_count: 3,
  quarantined_count: 0,
  created_at: "2026-11-27T10:00:00Z",
});
const ready = (doc_type: string, quarantined_count = 0) => ({
  document_id: "d",
  doc_type,
  chunk_count: 3,
  reused: false,
  quarantined_count,
  guardrails: [],
});
const prog = (step: string, status: string) => ev("progress", { step, status });

describe("Documents", () => {
  const fetchMock = vi.fn();
  let documents: unknown[];
  let post: (path: string, init: RequestInit) => Response | Promise<Response>;
  beforeEach(() => {
    documents = [];
    post = () => json(500, {});
    fetchMock.mockReset().mockImplementation((path: string, init: RequestInit = {}) => {
      if (path === "/api/documents") return Promise.resolve(json(200, { documents }));
      return Promise.resolve(post(path, init));
    });
    vi.stubGlobal("fetch", fetchMock);
  });
  afterEach(() => vi.unstubAllGlobals());

  it("shows current documents on mount and reports ready", async () => {
    documents = [doc("resume", "cv.pdf"), doc("jd", null)];
    const onReady = vi.fn();
    render(<Documents onReady={onReady} />);
    expect(await screen.findByText(/cv\.pdf/)).toBeInTheDocument();
    await waitFor(() => expect(onReady).toHaveBeenCalled());
  });

  it("sample button streams progress and calls onReady", async () => {
    const onReady = vi.fn();
    post = (path) => {
      expect(path).toBe("/api/samples");
      return sse(
        prog("validating", "started"),
        prog("validating", "done"),
        prog("parsing", "started"),
        ev("done", ready("resume")),
        ev("done", ready("jd")),
      );
    };
    const user = userEvent.setup();
    render(<Documents onReady={onReady} />);
    await screen.findByRole("button", { name: /try with sample/i });
    documents = [doc("resume", "sample-resume.pdf"), doc("jd", null)];
    await user.click(screen.getByRole("button", { name: /try with sample/i }));
    await waitFor(() => expect(onReady).toHaveBeenCalledTimes(1));
    expect(screen.getByText(/reading the file/i)).toBeInTheDocument();
  });

  it("disables JD submit under 50 characters and posts text at 50", async () => {
    post = () => sse(prog("validating", "started"), ev("done", ready("jd")));
    const user = userEvent.setup();
    render(<Documents onReady={vi.fn()} />);
    const box = await screen.findByLabelText(/job description/i);
    const submit = screen.getByRole("button", { name: /use this job description/i });
    await user.type(box, "too short");
    expect(submit).toBeDisabled();
    await user.clear(box);
    await user.type(box, "x".repeat(50));
    expect(submit).toBeEnabled();
    await user.click(submit);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/stream/documents", expect.anything()));
    const call = fetchMock.mock.calls.find((c) => c[0] === "/api/stream/documents")!;
    expect(JSON.parse(call[1].body)).toEqual({ doc_type: "jd", text: "x".repeat(50) });
  });

  it("rejects a non-PDF resume without any request", async () => {
    const user = userEvent.setup({ applyAccept: false });
    render(<Documents onReady={vi.fn()} />);
    const input = await screen.findByLabelText(/resume \(pdf/i);
    await user.upload(input, new File(["x"], "a.txt", { type: "text/plain" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Upload a PDF file.");
    expect(fetchMock.mock.calls.every((c) => c[0] === "/api/documents")).toBe(true);
  });

  it("uploads a resume then ingests it by key", async () => {
    post = (path) => {
      if (path === "/api/uploads")
        return json(200, { key: "k9", url: "/api/uploads/k9", method: "PUT", headers: { "Content-Type": "application/pdf" }, max_bytes: 5242880 });
      if (path === "/api/uploads/k9") return new Response(null, { status: 204 });
      return sse(prog("parsing", "started"), ev("done", ready("resume")));
    };
    const user = userEvent.setup();
    render(<Documents onReady={vi.fn()} />);
    const input = await screen.findByLabelText(/resume \(pdf/i);
    await user.upload(input, new File(["%PDF"], "me.pdf", { type: "application/pdf" }));
    await waitFor(() => {
      const call = fetchMock.mock.calls.find((c) => c[0] === "/api/stream/documents");
      expect(JSON.parse(call![1].body)).toEqual({ doc_type: "resume", upload_key: "k9", filename: "me.pdf" });
    });
  });

  it("shows a quarantine notice when content was held back", async () => {
    post = () => sse(prog("scanning", "done"), ev("done", ready("jd", 2)));
    const user = userEvent.setup();
    render(<Documents onReady={vi.fn()} />);
    await user.type(await screen.findByLabelText(/job description/i), "y".repeat(60));
    await user.click(screen.getByRole("button", { name: /use this job description/i }));
    expect(await screen.findByText(/held back/i)).toBeInTheDocument();
  });

  it("calls onReady again after a document is replaced", async () => {
    documents = [doc("resume", "cv.pdf"), doc("jd", null)];
    post = () => sse(prog("validating", "started"), ev("done", ready("jd")));
    const onReady = vi.fn();
    const user = userEvent.setup();
    render(<Documents onReady={onReady} />);
    await waitFor(() => expect(onReady).toHaveBeenCalledTimes(1));
    await user.type(await screen.findByLabelText(/job description/i), "y".repeat(50));
    await user.click(screen.getByRole("button", { name: /use this job description/i }));
    await waitFor(() => expect(onReady).toHaveBeenCalledTimes(2));
  });
});
