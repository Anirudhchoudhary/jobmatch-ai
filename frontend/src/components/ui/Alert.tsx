import type { ReactNode } from "react";

type Tone = "error" | "info";

const tones: Record<Tone, string> = {
  error: "border-danger/40 bg-danger-soft text-danger",
  info: "border-border bg-surface-muted text-foreground",
};

/** Errors use role="alert" (announced at once); info uses role="status" (polite). */
export function Alert({ tone = "error", children }: { tone?: Tone; children: ReactNode }) {
  return (
    <div
      role={tone === "error" ? "alert" : "status"}
      className={`rounded-lg border px-3 py-2.5 text-sm ${tones[tone]}`}
    >
      {children}
    </div>
  );
}
