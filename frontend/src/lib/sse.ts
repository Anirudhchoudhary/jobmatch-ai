import { ApiError, errorFromResponse } from "./api";

const interrupted = () =>
  new ApiError(0, "stream_interrupted", "The connection dropped. Please try again.");

interface ErrorPayload {
  code?: unknown;
  message?: unknown;
  request_id?: unknown;
}

/** Parse one SSE block ("event: x\ndata: {...}"); null when it has no data line. */
function parseBlock(block: string): { name: string; data: unknown } | null {
  let name = "message";
  const data: string[] = [];
  for (const line of block.split("\n")) {
    if (line.startsWith("event:")) name = line.slice(6).trim();
    else if (line.startsWith("data:")) data.push(line.slice(5).replace(/^ /, ""));
  }
  if (data.length === 0) return null;
  try {
    return { name, data: JSON.parse(data.join("\n")) };
  } catch {
    return null;
  }
}

/**
 * POST `body` as JSON and read the SSE response, calling `onEvent` per event.
 * Resolves after `doneCount` `done` events (default 1; the sample stream ingests two documents); rejects with ApiError on an `error` event, a non-2xx
 * response, or a body that ends without `done` or `error`.
 */
export async function streamSse(
  path: string,
  body: unknown,
  onEvent: (name: string, data: unknown) => void,
  signal?: AbortSignal,
  doneCount = 1,
): Promise<void> {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify(body ?? {}),
    credentials: "same-origin",
    signal,
  });
  if (!res.ok) throw await errorFromResponse(res);
  if (!res.body) throw interrupted();

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let finished = false;
  let dones = 0;

  const dispatch = (block: string): void => {
    const parsed = parseBlock(block);
    if (!parsed) return;
    if (parsed.name === "error") {
      const p = (parsed.data ?? {}) as ErrorPayload;
      throw new ApiError(
        0,
        typeof p.code === "string" ? p.code : "internal",
        typeof p.message === "string" ? p.message : "Something went wrong. Please try again.",
        typeof p.request_id === "string" ? p.request_id : undefined,
      );
    }
    onEvent(parsed.name, parsed.data);
    if (parsed.name === "done" && ++dones >= doneCount) finished = true;
  };

  try {
    while (!finished) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true }).replace(/\r\n?/g, "\n");
      let end: number;
      while (!finished && (end = buffer.indexOf("\n\n")) !== -1) {
        const block = buffer.slice(0, end);
        buffer = buffer.slice(end + 2);
        dispatch(block);
      }
    }
  } finally {
    // Free the connection on early finish, errors and aborts.
    reader.cancel().catch(() => undefined);
  }
  if (!finished) throw interrupted();
}
