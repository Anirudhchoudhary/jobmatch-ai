import { AlertTriangle, Check, Circle, Loader2 } from "lucide-react";

export type StatusItem = { label: string; state: "done" | "active" | "pending" | "warn" };

/** A one-line progress list; changes are announced politely to screen readers. */
export function StatusLine({ items }: { items: StatusItem[] }) {
  return (
    <div role="status" aria-live="polite">
      <ul className="flex flex-wrap items-center gap-x-4 gap-y-1 text-sm">
        {items.map((item) => (
          <li
            key={item.label}
            data-state={item.state}
            aria-current={item.state === "active" ? "step" : undefined}
            className={`inline-flex items-center gap-1.5 ${item.state === "pending" ? "text-muted" : ""}`}
          >
            {item.state === "done" ? (
              <Check aria-hidden="true" className="size-4 text-success" />
            ) : item.state === "warn" ? (
              <AlertTriangle aria-hidden="true" className="size-4 text-warning" />
            ) : item.state === "active" ? (
              <Loader2 aria-hidden="true" className="jm-spin size-4 text-brand" />
            ) : (
              <Circle aria-hidden="true" className="size-4" />
            )}
            {item.label}
          </li>
        ))}
      </ul>
    </div>
  );
}
