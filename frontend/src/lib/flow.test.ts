import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useFlow } from "./flow";

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
const notFound = (code: string) => json(404, { error: { code, message: "nope", request_id: "r" } });
const doc = (doc_type: string) => ({
  doc_id: `id-${doc_type}`,
  doc_type,
  source: "upload",
  filename: null,
  status: "ready",
  chunk_count: 3,
  quarantined_count: 0,
  created_at: "2026-11-27T10:00:00Z",
});

describe("useFlow", () => {
  const fetchMock = vi.fn();
  let documents: unknown[];
  let analysis: boolean;
  let suggestions: boolean;
  beforeEach(() => {
    documents = [];
    analysis = false;
    suggestions = false;
    fetchMock.mockReset().mockImplementation((path: string) => {
      if (path === "/api/documents") return Promise.resolve(json(200, { documents }));
      if (path === "/api/analyses/latest")
        return Promise.resolve(analysis ? json(200, { analysis_id: "a1" }) : notFound("analysis_not_found"));
      if (path === "/api/suggestions/latest")
        return Promise.resolve(suggestions ? json(200, { analysis_id: "a1" }) : notFound("suggestions_not_found"));
      return Promise.resolve(json(404, {}));
    });
    vi.stubGlobal("fetch", fetchMock);
  });
  afterEach(() => vi.unstubAllGlobals());

  async function restored() {
    const hook = renderHook(() => useFlow());
    await waitFor(() => expect(hook.result.current.restoring).toBe(false));
    return hook;
  }

  it("starts on documents with only documents unlocked when nothing is stored", async () => {
    const { result } = await restored();
    expect(result.current.step).toBe("documents");
    expect(result.current.arrival).toBe("restore");
    expect(result.current.unlocked).toEqual({ documents: true, match: false, improve: false, ask: false });
  });

  it.each([
    ["documents only", true, false, false, "match"],
    ["an analysis", true, true, false, "improve"],
    ["an analysis and suggestions", true, true, true, "ask"],
  ])("restores to the furthest step with %s, arrival restore", async (_name, docs, a, s, step) => {
    documents = docs ? [doc("resume"), doc("jd")] : [];
    analysis = a;
    suggestions = s;
    const { result } = await restored();
    expect(result.current.step).toBe(step);
    expect(result.current.arrival).toBe("restore");
    expect(result.current.done.documents).toBe(true);
    expect(result.current.done.match).toBe(a);
    expect(result.current.done.improve).toBe(s);
  });

  it("lands on the requested step instead of the furthest one, still as a restore", async () => {
    documents = [doc("resume"), doc("jd")];
    analysis = true;
    suggestions = true;
    const hook = renderHook(() => useFlow({ landOn: "match" }));
    await waitFor(() => expect(hook.result.current.restoring).toBe(false));
    expect(hook.result.current.step).toBe("match");
    expect(hook.result.current.arrival).toBe("restore");
    expect(hook.result.current.unlocked.ask).toBe(true);
  });

  it("falls back to the furthest open step when the requested one is locked", async () => {
    documents = [doc("resume"), doc("jd")];
    const hook = renderHook(() => useFlow({ landOn: "ask" }));
    await waitFor(() => expect(hook.result.current.restoring).toBe(false));
    expect(hook.result.current.step).toBe("match");
  });

  it("ignores a stored analysis when the documents are not both ready", async () => {
    documents = [doc("resume")];
    analysis = true;
    const { result } = await restored();
    expect(result.current.step).toBe("documents");
    expect(result.current.unlocked.improve).toBe(false);
  });

  it("advances to match with arrival user on the first documents-ready", async () => {
    const { result } = await restored();
    act(() => result.current.onDocumentsReady(true));
    expect(result.current.step).toBe("match");
    expect(result.current.arrival).toBe("user");
    expect(result.current.revisitingDocuments).toBe(false);
  });

  it("does not move when Documents reports the already-stored pair on mount", async () => {
    documents = [doc("resume"), doc("jd")];
    const { result } = await restored();
    act(() => result.current.goTo("documents"));
    act(() => result.current.onDocumentsReady(false));
    expect(result.current.step).toBe("documents");
    expect(result.current.docsVersion).toBe(0);
  });

  it("stays on documents when a document is replaced while revisiting, and clears match and improve", async () => {
    documents = [doc("resume"), doc("jd")];
    analysis = true;
    suggestions = true;
    const { result } = await restored();
    act(() => result.current.goTo("documents"));
    expect(result.current.revisitingDocuments).toBe(true);
    act(() => result.current.onDocumentsReady(true));
    expect(result.current.step).toBe("documents");
    expect(result.current.revisitingDocuments).toBe(true);
    expect(result.current.done.match).toBe(false);
    expect(result.current.done.improve).toBe(false);
    expect(result.current.unlocked.match).toBe(true);
    expect(result.current.unlocked.improve).toBe(false);
    expect(result.current.docsVersion).toBe(1);
  });

  it("goTo sets arrival user, and ignores locked steps", async () => {
    documents = [doc("resume"), doc("jd")];
    const { result } = await restored();
    act(() => result.current.goTo("improve"));
    expect(result.current.step).toBe("match");
    expect(result.current.arrival).toBe("restore");
    act(() => result.current.goTo("documents"));
    expect(result.current.step).toBe("documents");
    expect(result.current.arrival).toBe("user");
  });

  it("unlocks improve and ask once an analysis exists, without suggestions", async () => {
    documents = [doc("resume"), doc("jd")];
    const { result } = await restored();
    act(() => result.current.onAnalysis(true));
    expect(result.current.unlocked.improve).toBe(true);
    expect(result.current.unlocked.ask).toBe(true);
    expect(result.current.done.match).toBe(true);
    expect(result.current.done.improve).toBe(false);
    act(() => result.current.goTo("ask"));
    expect(result.current.step).toBe("ask");
    act(() => result.current.onSuggestions(true));
    expect(result.current.done.improve).toBe(true);
  });
  it("re-selecting the current step keeps arrival restore", async () => {
    documents = [doc("resume"), doc("jd")];
    const { result } = await restored();
    act(() => result.current.goTo("match"));
    expect(result.current.step).toBe("match");
    expect(result.current.arrival).toBe("restore");
  });

  it("falls back to the furthest open step, keeping arrival, when the current step locks", async () => {
    documents = [doc("resume"), doc("jd")];
    analysis = true;
    suggestions = true;
    const { result } = await restored();
    expect(result.current.step).toBe("ask");
    act(() => result.current.onAnalysis(false));
    expect(result.current.step).toBe("match");
    expect(result.current.arrival).toBe("restore");
  });

  it("lands on documents when the documents request fails", async () => {
    fetchMock.mockImplementation(() => Promise.resolve(json(500, {})));
    const { result } = await restored();
    expect(result.current.step).toBe("documents");
    expect(result.current.unlocked.match).toBe(false);
  });
});
