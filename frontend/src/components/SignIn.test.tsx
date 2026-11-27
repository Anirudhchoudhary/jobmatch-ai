import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SignIn } from "./SignIn";

const push = vi.fn();
const refresh = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ push, replace: push }) }));
vi.mock("@/lib/session", () => ({ useSession: () => ({ refresh }) }));

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}
const err = (code: string, message: string) => ({ error: { code, message, request_id: "r" } });

describe("SignIn", () => {
  const fetchMock = vi.fn();
  beforeEach(() => {
    fetchMock.mockReset();
    push.mockReset();
    refresh.mockReset().mockResolvedValue(true);
    vi.stubGlobal("fetch", fetchMock);
  });
  afterEach(() => vi.unstubAllGlobals());

  async function toCodeStep(user: ReturnType<typeof userEvent.setup>) {
    fetchMock.mockResolvedValueOnce(jsonResponse(202, { status: "sent" }));
    render(<SignIn />);
    await user.type(screen.getByLabelText(/email/i), "new@example.com");
    await user.click(screen.getByRole("button", { name: /send/i }));
    return screen.findByLabelText(/6-digit code/i);
  }

  it("shows the code step after submitting an email", async () => {
    const user = userEvent.setup();
    const code = await toCodeStep(user);
    expect(code).toHaveAttribute("inputmode", "numeric");
    expect(code).toHaveAttribute("autocomplete", "one-time-code");
    expect(code).toHaveAttribute("pattern", "[0-9]{6}");
    const [path, init] = fetchMock.mock.calls[0];
    expect(path).toBe("/api/auth/request-code");
    expect(JSON.parse(init.body)).toEqual({ email: "new@example.com" });
  });

  it("shows the server message for an invalid code", async () => {
    const user = userEvent.setup();
    const code = await toCodeStep(user);
    fetchMock.mockResolvedValueOnce(jsonResponse(400, err("invalid_code", "That code is not valid.")));
    await user.type(code, "123456");
    await user.click(screen.getByRole("button", { name: /verify/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent("That code is not valid.");
    expect(push).not.toHaveBeenCalled();
  });

  it("shows the server message on 429", async () => {
    const user = userEvent.setup();
    fetchMock.mockResolvedValueOnce(jsonResponse(429, err("too_many_requests", "Too many requests. Try later.")));
    render(<SignIn />);
    await user.type(screen.getByLabelText(/email/i), "a@b.co");
    await user.click(screen.getByRole("button", { name: /send/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Too many requests. Try later.");
  });

  it("navigates to /app/ after a successful verify", async () => {
    const user = userEvent.setup();
    const code = await toCodeStep(user);
    fetchMock.mockResolvedValueOnce(jsonResponse(200, { user_id: "u1" }));
    await user.type(code, "123456");
    await user.click(screen.getByRole("button", { name: /verify/i }));
    await waitFor(() => expect(push).toHaveBeenCalledWith("/app/"));
    const [path, init] = fetchMock.mock.calls[1];
    expect(path).toBe("/api/auth/verify");
    expect(JSON.parse(init.body)).toEqual({ email: "new@example.com", code: "123456" });
    expect(refresh).toHaveBeenCalled();
  });

  it("shows an error instead of navigating when the session cannot be loaded after verify", async () => {
    const user = userEvent.setup();
    const code = await toCodeStep(user);
    fetchMock.mockResolvedValueOnce(jsonResponse(200, { user_id: "u1" }));
    refresh.mockResolvedValueOnce(false);
    await user.type(code, "123456");
    await user.click(screen.getByRole("button", { name: /verify/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "You're signed in, but we couldn't load your account. Reload the page to continue.",
    );
    expect(push).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: /verify/i })).toBeEnabled();
  });

  it("resends the code", async () => {
    const user = userEvent.setup();
    await toCodeStep(user);
    fetchMock.mockResolvedValueOnce(jsonResponse(202, { status: "sent" }));
    await user.click(screen.getByRole("button", { name: /resend/i }));
    expect(await screen.findByRole("status")).toHaveTextContent(/sent/i);
    expect(fetchMock.mock.calls[1][0]).toBe("/api/auth/request-code");
  });
});
