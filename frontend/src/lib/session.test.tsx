import { act, render, screen, waitFor } from "@testing-library/react";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SessionProvider, SESSION_EXPIRED_MESSAGE, SIGN_OUT_FAILED_MESSAGE, useSession } from "./session";
import { apiGet } from "./api";

const push = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ push, replace: push }) }));

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const authRequired = { error: { code: "auth_required", message: "Sign in to continue.", request_id: "r" } };

function Probe() {
  const { user, loading, notice, signOutError, refresh, signOut } = useSession();
  const [refreshed, setRefreshed] = useState("");
  return (
    <div>
      <p data-testid="state">{loading ? "loading" : (user?.email ?? "signed-out")}</p>
      <p data-testid="notice">{notice ?? ""}</p>
      <p data-testid="signout-error">{signOutError ?? ""}</p>
      <p data-testid="refreshed">{refreshed}</p>
      <button onClick={() => void signOut()}>out</button>
      <button onClick={() => void refresh().then((ok) => setRefreshed(String(ok)))}>again</button>
    </div>
  );
}

describe("session", () => {
  const fetchMock = vi.fn();
  beforeEach(() => {
    fetchMock.mockReset();
    push.mockReset();
    vi.stubGlobal("fetch", fetchMock);
  });
  afterEach(() => vi.unstubAllGlobals());

  it("loads the current user on mount", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, { user_id: "u1", email: "a@b.co" }));
    render(<SessionProvider><Probe /></SessionProvider>);
    await waitFor(() => expect(screen.getByTestId("state")).toHaveTextContent("a@b.co"));
    expect(fetchMock.mock.calls[0][0]).toBe("/api/auth/me");
  });

  it("is signed out, without an expiry notice, when the first /me is 401", async () => {
    fetchMock.mockResolvedValue(jsonResponse(401, authRequired));
    render(<SessionProvider><Probe /></SessionProvider>);
    await waitFor(() => expect(screen.getByTestId("state")).toHaveTextContent("signed-out"));
    expect(screen.getByTestId("notice")).toHaveTextContent("");
    expect(push).not.toHaveBeenCalled();
  });

  it("clears the user and routes to / on a later 401 auth_required", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(200, { user_id: "u1", email: "a@b.co" }));
    render(<SessionProvider><Probe /></SessionProvider>);
    await waitFor(() => expect(screen.getByTestId("state")).toHaveTextContent("a@b.co"));

    fetchMock.mockResolvedValueOnce(jsonResponse(401, authRequired));
    await act(async () => {
      await apiGet("/api/documents").catch(() => undefined);
    });
    expect(screen.getByTestId("state")).toHaveTextContent("signed-out");
    expect(screen.getByTestId("notice")).toHaveTextContent(SESSION_EXPIRED_MESSAGE);
    expect(SESSION_EXPIRED_MESSAGE).toBe("Your session expired. Sign in again.");
    expect(push).toHaveBeenCalledWith("/");
  });

  it("signOut posts logout (204) and clears the user", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(200, { user_id: "u1", email: "a@b.co" }));
    render(<SessionProvider><Probe /></SessionProvider>);
    await waitFor(() => expect(screen.getByTestId("state")).toHaveTextContent("a@b.co"));

    fetchMock.mockResolvedValueOnce(new Response(null, { status: 204 }));
    await act(async () => {
      screen.getByText("out").click();
    });
    await waitFor(() => expect(screen.getByTestId("state")).toHaveTextContent("signed-out"));
    const [path, init] = fetchMock.mock.calls[1];
    expect(path).toBe("/api/auth/logout");
    expect(init.method).toBe("POST");
    expect(screen.getByTestId("notice")).toHaveTextContent("");
    expect(push).toHaveBeenCalledWith("/");
  });

  it("keeps the user signed in and shows an error when logout fails", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(200, { user_id: "u1", email: "a@b.co" }));
    render(<SessionProvider><Probe /></SessionProvider>);
    await waitFor(() => expect(screen.getByTestId("state")).toHaveTextContent("a@b.co"));

    fetchMock.mockResolvedValueOnce(
      jsonResponse(503, { error: { code: "provider_unavailable", message: "Down.", request_id: "r" } }),
    );
    await act(async () => {
      screen.getByText("out").click();
    });
    await waitFor(() =>
      expect(screen.getByTestId("signout-error")).toHaveTextContent(SIGN_OUT_FAILED_MESSAGE),
    );
    expect(SIGN_OUT_FAILED_MESSAGE).toBe("Sign-out failed. Please try again.");
    expect(screen.getByTestId("state")).toHaveTextContent("a@b.co");
    expect(push).not.toHaveBeenCalled();
  });

  it("keeps the user signed in when the logout request cannot be sent", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(200, { user_id: "u1", email: "a@b.co" }));
    render(<SessionProvider><Probe /></SessionProvider>);
    await waitFor(() => expect(screen.getByTestId("state")).toHaveTextContent("a@b.co"));

    fetchMock.mockRejectedValueOnce(new TypeError("Failed to fetch"));
    await act(async () => {
      screen.getByText("out").click();
    });
    await waitFor(() =>
      expect(screen.getByTestId("signout-error")).toHaveTextContent(SIGN_OUT_FAILED_MESSAGE),
    );
    expect(screen.getByTestId("state")).toHaveTextContent("a@b.co");

    // A later successful sign-out clears the error.
    fetchMock.mockResolvedValueOnce(new Response(null, { status: 204 }));
    await act(async () => {
      screen.getByText("out").click();
    });
    await waitFor(() => expect(screen.getByTestId("state")).toHaveTextContent("signed-out"));
    expect(screen.getByTestId("signout-error")).toHaveTextContent("");
  });

  it("refresh resolves true when signed in and false otherwise", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(401, authRequired));
    render(<SessionProvider><Probe /></SessionProvider>);
    await waitFor(() => expect(screen.getByTestId("state")).toHaveTextContent("signed-out"));

    fetchMock.mockResolvedValueOnce(jsonResponse(500, {}));
    await act(async () => {
      screen.getByText("again").click();
    });
    await waitFor(() => expect(screen.getByTestId("refreshed")).toHaveTextContent("false"));

    fetchMock.mockResolvedValueOnce(jsonResponse(200, { user_id: "u1", email: "a@b.co" }));
    await act(async () => {
      screen.getByText("again").click();
    });
    await waitFor(() => expect(screen.getByTestId("refreshed")).toHaveTextContent("true"));
  });
});
