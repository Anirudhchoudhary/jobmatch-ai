import { execFileSync } from "node:child_process";

// The local console email sender logs the code as a JSON line `login_code_console`.
// Read the newest such line from the API container (no test-only API hook).
export function latestLoginCode(since: Date): string {
  const out = execFileSync(
    "docker",
    ["compose", "logs", "api", "--no-log-prefix", "--since", since.toISOString()],
    { encoding: "utf8", cwd: "..", maxBuffer: 32 * 1024 * 1024 },
  );
  const lines = out.split("\n").filter((l) => l.includes("login_code_console"));
  for (const line of lines.reverse()) {
    try {
      const entry = JSON.parse(line.slice(line.indexOf("{"))) as { event?: string; code?: string };
      if (entry.event === "login_code_console" && entry.code) return entry.code;
    } catch {
      // not a JSON line; keep looking
    }
  }
  throw new Error("no login_code_console line in the API logs yet");
}
