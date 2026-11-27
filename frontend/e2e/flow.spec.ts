import { expect, test } from "@playwright/test";
import { latestLoginCode } from "./login-code";

function codeOrNull(since: Date): string | null {
  try {
    return latestLoginCode(since);
  } catch {
    return null;
  }
}

test("sign in, sample, analysis, chat, guardrails, reload, sign out", async ({ page }) => {
  // Back-date a little so clock skew between host and container cannot hide the line.
  const requestedAt = new Date(Date.now() - 5_000);
  const email = `e2e+${Date.now()}@example.com`;

  await page.goto("/");
  await page.getByLabel("Email address").fill(email);
  await page.getByRole("button", { name: "Send sign-in code" }).click();

  const codeInput = page.getByLabel("6-digit code");
  await expect(codeInput).toBeVisible();
  await expect
    .poll(() => codeOrNull(requestedAt), { message: "waiting for the sign-in code in the API logs" })
    .not.toBeNull();
  await codeInput.fill(latestLoginCode(requestedAt));
  await page.getByRole("button", { name: "Verify and continue" }).click();

  await expect(page).toHaveURL(/\/app\/$/);
  await page.getByRole("button", { name: "Try with sample resume + JD" }).click();
  await expect(page.getByText("Job description", { exact: true })).toBeVisible();

  await page.getByRole("button", { name: "Run analysis" }).click();
  const score = page.getByRole("meter", { name: "Match score" });
  await expect(score).toBeVisible();
  await expect(score).toHaveAttribute("aria-valuenow", /^\d+(\.\d+)?$/);

  await page.getByLabel("Ask a question").fill("Which required skills am I missing?");
  await page.getByRole("button", { name: "Send" }).click();
  // The offline fake model's answer is not asserted on; only that a turn completes.
  await expect(page.getByRole("listitem", { name: "Conversation turn" })).toHaveCount(1);
  const guardrails = page.getByRole("button", { name: "Show guardrails" });
  await expect(guardrails).toBeVisible();
  await guardrails.click();
  await expect(page.getByRole("list", { name: "Guardrail checks" }).getByRole("listitem").first()).toBeVisible();

  await page.reload();
  await expect(page.getByRole("meter", { name: "Match score" })).toBeVisible();

  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(page.getByLabel("Email address")).toBeVisible();
});
