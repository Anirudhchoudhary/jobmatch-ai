import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "./api";
import { createDemoBackend, DEMO_FREE_TEXT_ANSWER, DEMO_SIGN_IN_MESSAGE, type DemoRecording } from "./demoBackend";
import recording from "@/demo/recording.json";

const rec = recording as unknown as DemoRecording;
const FAST = { step: 1, token: 1 };

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn(() => Promise.reject(new Error("the demo must not use the network"))));
});
afterEach(() => vi.unstubAllGlobals());

async function collect(path: string, body: unknown = {}) {
  const events: [string, unknown][] = [];
  await createDemoBackend(rec, FAST).stream(path, body, (n, d) => events.push([n, d]));
  return events;
}

describe("createDemoBackend", () => {
  it("serves the recorded documents, analysis, suggestions and chunks", async () => {
    const b = createDemoBackend(rec, FAST);
    const docs = await b.get<{ documents: { doc_type: string }[] }>("/api/documents");
    expect(docs.documents.map((d) => d.doc_type).sort()).toEqual(["jd", "resume"]);
    const analysis = await b.get<{ score: number; skills: { evidence: { chunk_id: string }[] }[] }>("/api/analyses/latest");
    expect(analysis.score).toBeGreaterThan(0);
    const chunkId = analysis.skills.flatMap((s) => s.evidence)[0].chunk_id;
    expect((await b.get<{ text: string }>(`/api/chunks/${chunkId}`)).text.length).toBeGreaterThan(0);
    await expect(b.get("/api/suggestions/latest")).resolves.toBeTruthy();
    expect(fetch).not.toHaveBeenCalled();
  });

  it("answers anything else with a 404", async () => {
    await expect(createDemoBackend(rec, FAST).get("/api/chunks/nope")).rejects.toMatchObject({ status: 404 });
  });

  it("replays the analysis with its progress steps and a final done", async () => {
    const events = await collect("/api/stream/analyses");
    expect(events.filter(([n]) => n === "progress").length).toBeGreaterThan(1);
    expect(events.at(-1)?.[0]).toBe("done");
  });

  it("replays a recorded chat answer for a starter question, whatever its spacing or case", async () => {
    const q = rec.chats[0].question;
    const events = await collect("/api/stream/chat", { question: `  ${q.toUpperCase()} ` });
    const done = events.at(-1)?.[1] as { answer: string };
    expect(events.some(([n]) => n === "token")).toBe(true);
    expect(done.answer).toBe((rec.chats[0].events.at(-1)?.data as { answer: string }).answer);
  });

  it("offers the starter questions from the recording, including the injection example", () => {
    const starters = createDemoBackend(rec, FAST).starters ?? [];
    expect(starters).toEqual(rec.chats.map((c) => c.question));
    expect(starters.some((s) => /ignore your previous instructions/i.test(s))).toBe(true);
  });

  it("explains, instead of failing, when asked something it has no recording for", async () => {
    const events = await collect("/api/stream/chat", { question: "What is my salary?" });
    const done = events.at(-1)?.[1] as { answer: string; refused: boolean };
    expect(done).toMatchObject({ answer: DEMO_FREE_TEXT_ANSWER, refused: true });
  });

  it("refuses uploads and ingestion with a sign-in message", async () => {
    const b = createDemoBackend(rec, FAST);
    await expect(b.upload(new File(["x"], "r.pdf"))).rejects.toEqual(new ApiError(0, "demo", DEMO_SIGN_IN_MESSAGE));
    await expect(b.stream("/api/stream/documents", {}, () => {})).rejects.toMatchObject({ message: DEMO_SIGN_IN_MESSAGE });
  });

  it("stops a replay when aborted", async () => {
    const ctrl = new AbortController();
    const seen: string[] = [];
    const p = createDemoBackend(rec, { step: 50, token: 50 }).stream("/api/stream/analyses", {}, (n) => seen.push(n), ctrl.signal);
    ctrl.abort();
    await expect(p).rejects.toMatchObject({ name: "AbortError" });
    expect(seen).toEqual([]);
  });
});
