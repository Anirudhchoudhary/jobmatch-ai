import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, setUnauthorizedHandler } from "./api";
import { streamSse } from "./sse";

const encoder = new TextEncoder();

function streamResponse(chunks: string[], status = 200): Response {
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
      controller.close();
    },
  });
  return new Response(body, { status, headers: { "Content-Type": "text/event-stream" } });
}

function ev(name: string, data: unknown): string {
  return `event: ${name}\ndata: ${JSON.stringify(data)}\n\n`;
}

async function collect() {
  const events: [string, unknown][] = [];
  await streamSse("/api/stream/chat", { question: "hi" }, (n, d) => events.push([n, d]));
  return events;
}

describe("streamSse", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
    setUnauthorizedHandler(null);
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("posts JSON and parses multiple events in one chunk", async () => {
    fetchMock.mockResolvedValue(
      streamResponse([ev("progress", { step: "retrieving" }) + ev("done", { answer: "x" })]),
    );
    const events = await collect();
    expect(events).toEqual([
      ["progress", { step: "retrieving" }],
      ["done", { answer: "x" }],
    ]);
    const [path, init] = fetchMock.mock.calls[0];
    expect(path).toBe("/api/stream/chat");
    expect(init.method).toBe("POST");
    expect(init.credentials).toBe("same-origin");
    expect(new Headers(init.headers).get("Content-Type")).toBe("application/json");
    expect(init.body).toBe('{"question":"hi"}');
  });

  it("reassembles events split across chunks mid-line", async () => {
    const full = ev("token", { text: "héllo" }) + ev("done", { answer: "héllo" });
    const bytes = encoder.encode(full);
    // Split inside a multi-byte character and mid-line.
    const cut = full.indexOf("é") + 1;
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(bytes.slice(0, 9));
        controller.enqueue(bytes.slice(9, cut));
        controller.enqueue(bytes.slice(cut, cut + 1));
        controller.enqueue(bytes.slice(cut + 1));
        controller.close();
      },
    });
    fetchMock.mockResolvedValue(new Response(body, { status: 200 }));
    const events = await collect();
    expect(events).toEqual([
      ["token", { text: "héllo" }],
      ["done", { answer: "héllo" }],
    ]);
  });

  it("handles CRLF line endings", async () => {
    fetchMock.mockResolvedValue(
      streamResponse(['event: done\r\ndata: {"answer":"x"}\r\n\r\n']),
    );
    expect(await collect()).toEqual([["done", { answer: "x" }]]);
  });

  it("turns an error event into a thrown ApiError with status 0", async () => {
    fetchMock.mockResolvedValue(
      streamResponse([
        ev("progress", { step: "generating" }),
        ev("error", { code: "provider_unavailable", message: "Try again shortly.", request_id: "r9" }),
      ]),
    );
    const seen: string[] = [];
    const err = await streamSse("/api/stream/chat", {}, (n) => seen.push(n)).catch(
      (e: unknown) => e,
    );
    expect(err).toBeInstanceOf(ApiError);
    expect(err).toMatchObject({
      status: 0,
      code: "provider_unavailable",
      message: "Try again shortly.",
      requestId: "r9",
    });
    expect(seen).toEqual(["progress"]);
  });

  it("throws stream_interrupted when the body ends without done or error", async () => {
    fetchMock.mockResolvedValue(streamResponse([ev("token", { text: "a" })]));
    await expect(collect()).rejects.toMatchObject({
      code: "stream_interrupted",
      message: "The connection dropped. Please try again.",
    });
  });

  it("throws stream_interrupted for a truncated trailing event", async () => {
    fetchMock.mockResolvedValue(streamResponse(['event: done\ndata: {"answer"']));
    await expect(collect()).rejects.toMatchObject({ code: "stream_interrupted" });
  });

  it("throws ApiError with the server message on a non-2xx JSON error", async () => {
    fetchMock.mockResolvedValue(
      new Response(
        JSON.stringify({ error: { code: "rate_limited", message: "Daily limit reached.", request_id: "r1" } }),
        { status: 429, headers: { "Content-Type": "application/json" } },
      ),
    );
    await expect(collect()).rejects.toMatchObject({
      status: 429,
      code: "rate_limited",
      message: "Daily limit reached.",
    });
  });

  it("calls the unauthorized handler on a 401 before streaming", async () => {
    const handler = vi.fn();
    setUnauthorizedHandler(handler);
    fetchMock.mockResolvedValue(
      new Response(
        JSON.stringify({ error: { code: "auth_required", message: "Sign in.", request_id: "r2" } }),
        { status: 401 },
      ),
    );
    await expect(collect()).rejects.toMatchObject({ code: "auth_required" });
    expect(handler).toHaveBeenCalledTimes(1);
  });

  it("passes the abort signal to fetch", async () => {
    fetchMock.mockResolvedValue(streamResponse([ev("done", {})]));
    const controller = new AbortController();
    await streamSse("/api/x", {}, () => {}, controller.signal);
    expect(fetchMock.mock.calls[0][1].signal).toBe(controller.signal);
  });
});
