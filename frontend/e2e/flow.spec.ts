import { expect, test } from "@playwright/test";
import { latestLoginCode } from "./login-code";

function codeOrNull(since: Date): string | null {
  try {
    return latestLoginCode(since);
  } catch {
    return null;
  }
}

test("sign in, sample, match, improve, ask, guardrails, reload, sign out", async ({ page }) => {
  const email = `e2e+${Date.now()}@example.com`;

  await page.goto("/");
  await page.getByLabel("Email address").fill(email);
  const requestedAt = new Date();
  await page.getByRole("button", { name: "Send sign-in code" }).click();

  const codeInput = page.getByLabel("6-digit code");
  await expect(codeInput).toBeVisible();
  let code = "";
  await expect
    .poll(() => (code = codeOrNull(requestedAt) ?? ""), {
      message: "waiting for the sign-in code in the API logs",
    })
    .toMatch(/^\d{6}$/);
  await codeInput.fill(code);
  await page.getByRole("button", { name: "Verify and continue" }).click();

  await expect(page).toHaveURL(/\/app\/$/);
  await page.getByRole("button", { name: "Try the sample" }).click();

  // The sample ingests, then Step 1 advances to Match by itself and the analysis runs unprompted.
  await expect(page.getByRole("heading", { level: 1, name: /step 2/i })).toBeVisible();
  const score = page.getByRole("meter", { name: "Match score" });
  await expect(score).toBeVisible();
  await expect(score).toHaveAttribute("aria-valuenow", /^\d+(\.\d+)?$/);

  await page.getByRole("button", { name: /^Next/ }).click();
  await expect(page.getByRole("heading", { level: 1, name: /step 3/i })).toBeVisible();
  // Suggestions auto-run; wait for the rewrites to land before moving on.
  await expect(page.getByRole("button", { name: "Suggest again" })).toBeVisible();
  await page.getByRole("button", { name: /^Next/ }).click();
  await expect(page.getByRole("heading", { level: 1, name: /step 4/i })).toBeVisible();

  await page.getByRole("button", { name: "What's my biggest gap?" }).click();
  // The offline fake model's answer text is not asserted on; only that a turn completes with its UI.
  const turn = page.getByRole("listitem", { name: "Conversation turn" });
  await expect(turn).toHaveCount(1);
  // The Sources row appears when the answer cites anything (labels are never shown raw).
  await expect(turn.getByText("Sources", { exact: true })).toBeVisible();
  await expect(turn.getByRole("button", { name: /^1 Resume · / })).toBeVisible();
  await expect(turn.locator("p.whitespace-pre-wrap")).not.toContainText("[r1]");
  const guardrails = page.getByRole("button", { name: "Show guardrails" });
  await expect(guardrails).toBeVisible();
  await guardrails.click();
  await expect(page.getByRole("list", { name: "Guardrail checks" }).getByRole("listitem").first()).toBeVisible();

  // Reloading restores the steps without spending any new analysis or suggestion run.
  const streamPosts: string[] = [];
  page.on("request", (r) => {
    if (r.method() === "POST" && /\/api\/stream\/(analyses|suggestions)/.test(r.url())) streamPosts.push(r.url());
  });
  await page.reload();
  const steps = page.getByRole("navigation", { name: /progress/i });
  await expect(page.getByRole("heading", { level: 1, name: /step 4/i })).toBeVisible();
  await steps.getByRole("button", { name: /improve/i }).click();
  await expect(page.getByRole("heading", { level: 1, name: /step 3/i })).toBeVisible();
  await steps.getByRole("button", { name: /match/i }).click();
  await expect(page.getByRole("meter", { name: "Match score" })).toBeVisible();
  expect(streamPosts).toEqual([]);

  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(page.getByLabel("Email address")).toBeVisible();
});
