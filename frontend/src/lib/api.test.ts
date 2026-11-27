import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, apiGet, apiPost, setUnauthorizedHandler } from "./api";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function errorBody(code: string, message: string) {
  return { error: { code, message, request_id: "req-1" } };
}

describe("api client", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
    setUnauthorizedHandler(null);
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("posts JSON with a JSON content type and an empty object body by default", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, { ok: true }));
    await expect(apiPost("/api/auth/logout")).resolves.toEqual({ ok: true });
    const [path, init] = fetchMock.mock.calls[0];
    expect(path).toBe("/api/auth/logout");
    expect(init.method).toBe("POST");
    expect(init.credentials).toBe("same-origin");
    expect(new Headers(init.headers).get("Content-Type")).toBe("application/json");
    expect(init.body).toBe("{}");
  });

  it("serialises a provided body", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, {}));
    await apiPost("/api/auth/verify", { code: "123456" });
    expect(fetchMock.mock.calls[0][1].body).toBe('{"code":"123456"}');
  });

  it("gets with same-origin credentials", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, { a: 1 }));
    await expect(apiGet("/api/auth/me")).resolves.toEqual({ a: 1 });
    expect(fetchMock.mock.calls[0][1].credentials).toBe("same-origin");
  });

  it("surfaces a 429 message verbatim", async () => {
    const message = "You have reached the daily limit. Try again tomorrow.";
    fetchMock.mockResolvedValue(jsonResponse(429, errorBody("rate_limited", message)));
    const err = await apiPost("/api/x").catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err).toMatchObject({ status: 429, code: "rate_limited", requestId: "req-1", message });
  });

  it("surfaces a 503 demo_at_capacity message verbatim", async () => {
    const message = "The demo is at capacity right now. Please try again later.";
    fetchMock.mockResolvedValue(jsonResponse(503, errorBody("demo_at_capacity", message)));
    const err = await apiGet("/api/x").catch((e: unknown) => e);
    expect(err).toMatchObject({ status: 503, code: "demo_at_capacity", message });
  });

  it("falls back to a generic message when the body is not an error envelope", async () => {
    fetchMock.mockResolvedValue(new Response("<html>bad gateway</html>", { status: 502 }));
    const err = await apiGet("/api/x").catch((e: unknown) => e);
    expect(err).toMatchObject({
      status: 502,
      message: "Something went wrong. Please try again.",
    });
  });

  it("calls the unauthorized handler on 401 auth_required and still throws", async () => {
    const handler = vi.fn();
    setUnauthorizedHandler(handler);
    fetchMock.mockResolvedValue(
      jsonResponse(401, errorBody("auth_required", "Please sign in to continue.")),
    );
    await expect(apiGet("/api/auth/me")).rejects.toMatchObject({ code: "auth_required" });
    expect(handler).toHaveBeenCalledTimes(1);
  });

  it("does not call the handler for other errors", async () => {
    const handler = vi.fn();
    setUnauthorizedHandler(handler);
    fetchMock.mockResolvedValue(jsonResponse(400, errorBody("invalid_code", "Wrong code.")));
    await expect(apiPost("/api/auth/verify", {})).rejects.toBeInstanceOf(ApiError);
    expect(handler).not.toHaveBeenCalled();
  });
});
