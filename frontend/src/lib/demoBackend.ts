import { ApiError } from "./api";
import type { Backend, StreamHandler } from "./backend";
import type { ChatDone, ChunkText, DocumentList } from "./types";

/** One recorded server-sent event. `t` (ms since the request started) is kept but not used. */
export interface RecordedEvent {
  t: number;
  event: string;
  data: unknown;
}

/** A real run of the sample resume and job description, captured by `make record-demo`. */
export interface DemoRecording {
  recorded_at: string;
  documents: DocumentList;
  analysis: RecordedEvent[];
  suggestions: RecordedEvent[];
  chats: { question: string; events: RecordedEvent[] }[];
  chunks: Record<string, ChunkText>;
}

/** Replay pacing in ms: a pause before each progress step, and between streamed answer pieces. */
export interface DemoPacing {
  step: number;
  token: number;
}

export const DEMO_PACING: DemoPacing = { step: 600, token: 35 };

export const DEMO_SIGN_IN_MESSAGE = "This is a recorded demo. Sign in to use your own documents.";

export const DEMO_FREE_TEXT_ANSWER =
  "This demo replays recorded answers, so it can only answer the suggested questions. Sign in to ask your own.";

const doneOf = (events: RecordedEvent[]) => [...events].reverse().find((e) => e.event === "done")?.data;

const aborted = () => new DOMException("The demo replay was stopped.", "AbortError");

function wait(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) return reject(aborted());
    const timer = setTimeout(resolve, ms);
    signal?.addEventListener(
      "abort",
      () => {
        clearTimeout(timer);
        reject(aborted());
      },
      { once: true },
    );
  });
}

/** A chat reply for a question the recording does not have. */
function freeTextReply(): RecordedEvent[] {
  const done: Omit<ChatDone, "request_id"> = {
    answer: DEMO_FREE_TEXT_ANSWER,
    citations: [],
    invalid_citations: [],
    refused: true,
    prompt_version: "demo",
    guardrails: [],
    guardrail_checks: [],
    blocked: false,
    cached: false,
  };
  return [
    { t: 0, event: "progress", data: { step: "checking_input" } },
    { t: 0, event: "token", data: { text: DEMO_FREE_TEXT_ANSWER } },
    { t: 0, event: "done", data: done },
  ];
}

const normalise = (q: string) => q.trim().toLowerCase().replace(/\s+/g, " ");

/**
 * A backend that never touches the network: reads come from the recording, and streams replay the
 * recorded events with steady pacing so the progress steps and typing look as they do live.
 * Anything that would create data is refused with a "sign in" message.
 */
export function createDemoBackend(recording: DemoRecording, pacing: DemoPacing = DEMO_PACING): Backend {
  const reads: Record<string, unknown> = {
    "/api/documents": recording.documents,
    "/api/analyses/latest": doneOf(recording.analysis),
    "/api/suggestions/latest": doneOf(recording.suggestions),
  };
  const chats = new Map(recording.chats.map((c) => [normalise(c.question), c.events]));

  async function replay(events: RecordedEvent[], onEvent: StreamHandler, signal?: AbortSignal) {
    for (const e of events) {
      await wait(e.event === "token" ? pacing.token : e.event === "progress" ? pacing.step : 0, signal);
      onEvent(e.event, e.data);
    }
  }

  return {
    demo: true,
    starters: recording.chats.map((c) => c.question),

    get<T>(path: string): Promise<T> {
      const chunk = path.match(/^\/api\/chunks\/([^/]+)$/);
      const data = chunk ? recording.chunks[chunk[1]] : reads[path];
      if (data === undefined) return Promise.reject(new ApiError(404, "not_found", "That isn't part of the demo."));
      return Promise.resolve(data as T);
    },

    stream(path, body, onEvent, signal) {
      if (path === "/api/stream/analyses") return replay(recording.analysis, onEvent, signal);
      if (path === "/api/stream/suggestions") return replay(recording.suggestions, onEvent, signal);
      if (path === "/api/stream/chat") {
        const question = String((body as { question?: unknown } | null)?.question ?? "");
        return replay(chats.get(normalise(question)) ?? freeTextReply(), onEvent, signal);
      }
      return Promise.reject(new ApiError(0, "demo", DEMO_SIGN_IN_MESSAGE));
    },

    upload() {
      return Promise.reject(new ApiError(0, "demo", DEMO_SIGN_IN_MESSAGE));
    },
  };
}
