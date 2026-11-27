export type ProgressStatus = "started" | "done";

const STEPS: [string, string][] = [
  ["validating", "Checking the file"],
  ["parsing", "Reading the file"],
  ["chunking", "Splitting into sections"],
  ["scanning", "Scanning for unsafe content"],
  ["embedding", "Indexing for search"],
  ["storing", "Saving"],
];

/** Pipeline steps as they stream in; `steps` maps a backend step name to its latest status. */
export function Progress({ steps }: { steps: Record<string, ProgressStatus> }) {
  return (
    <ol aria-label="Progress" className="flex flex-col gap-1 text-sm">
      {STEPS.filter(([key]) => steps[key]).map(([key, label]) => (
        <li key={key} className={steps[key] === "done" ? "text-muted" : "font-medium"}>
          <span aria-hidden="true">{steps[key] === "done" ? "✓ " : "… "}</span>
          {label}
          <span className="sr-only">{steps[key] === "done" ? " (done)" : " (in progress)"}</span>
        </li>
      ))}
    </ol>
  );
}
