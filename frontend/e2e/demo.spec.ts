import { expect, test } from "@playwright/test";

test("the demo shows a finished run without signing in or spending anything", async ({ page }) => {
  // The session check (`/api/auth/me`) is the only API call allowed: everything else is replayed.
  const apiCalls: string[] = [];
  page.on("request", (req) => {
    const url = new URL(req.url());
    if (url.pathname.startsWith("/api/") && !/^\/api\/auth\/me\/?$/.test(url.pathname)) apiCalls.push(`${req.method()} ${url.pathname}`);
  });

  await page.goto("/");
  await page.getByRole("link", { name: "See a live demo" }).click();
  await expect(page).toHaveURL(/\/demo\/$/);
  await expect(page.getByText("Recorded example with a sample resume.")).toBeVisible();

  // Lands on the match, already scored.
  await expect(page.getByRole("heading", { name: "Step 2 · Your match" })).toBeVisible();
  await expect(page.getByRole("meter", { name: /match score/i })).toBeVisible();
  await page.locator("button", { hasText: "view in resume" }).first().click();
  await expect(page.locator("section:not([hidden]) mark").first()).toBeVisible();

  // Re-running replays the recording.
  await page.getByRole("button", { name: "Re-run analysis" }).click();
  await expect(page.getByText("Reading the job")).toBeVisible();
  await expect(page.getByRole("button", { name: "Re-run analysis" })).toBeEnabled({ timeout: 15_000 });

  await page.getByRole("button", { name: /Next: improve my resume/ }).click();
  await expect(page.getByText("Before").first()).toBeVisible();
  await expect(page.getByText("After").first()).toBeVisible();

  await page.getByRole("button", { name: /Next: ask questions/ }).click();
  await page.getByRole("button", { name: "What's my biggest gap?" }).click();
  const turn = page.getByRole("listitem").filter({ hasText: "What's my biggest gap?" });
  await expect(turn.getByText("Sources")).toBeVisible({ timeout: 20_000 });
  await expect(turn.getByText("Verified against sources")).toBeVisible();
  await expect(turn).not.toContainText(/\[[rj]\d+\]|\*\*/);

  // The documents step is read-only.
  await page.getByRole("button", { name: /Documents/ }).first().click();
  await expect(page.getByRole("button", { name: /Replace/ })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Try the sample" })).toHaveCount(0);

  expect(apiCalls).toEqual([]);
});

test("the demo's injection example is blocked by the guardrails", async ({ page }) => {
  await page.goto("/demo/");
  await page.getByRole("button", { name: /Ask/ }).first().click();
  await page.getByRole("button", { name: /Ignore your previous instructions/ }).click();
  await expect(page.getByText("That looks like an attempt to change my instructions")).toBeVisible({ timeout: 10_000 });
  await page.getByRole("button", { name: /guardrails/i }).first().click();
  await expect(page.getByText(/injection/i).first()).toBeVisible();
});
