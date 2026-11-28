import { Check } from "lucide-react";
import { STEPS, type StepId } from "@/lib/flow";

const LABELS: Record<StepId, string> = {
  documents: "Documents",
  match: "Match",
  improve: "Improve",
  ask: "Ask",
};

const HINTS: Record<StepId, string> = {
  documents: "",
  match: "Add both documents first",
  improve: "Needs your match first",
  ask: "Needs your match first",
};

/** The progress bar: finished steps are ticked and clickable, locked steps are disabled with a hint. */
export function Stepper({
  step,
  unlocked,
  done,
  onSelect,
}: {
  step: StepId;
  unlocked: Record<StepId, boolean>;
  done: Record<StepId, boolean>;
  onSelect(step: StepId): void;
}) {
  return (
    <nav aria-label="Progress">
      <ol className="grid grid-cols-4 gap-1">
        {STEPS.map((id, i) => {
          const current = id === step;
          const locked = !unlocked[id];
          const hintId = `step-hint-${id}`;
          const reached = done[id] || current;
          return (
            <li key={id} className="relative flex flex-col items-center gap-1 text-center">
              {i > 0 ? (
                <span
                  aria-hidden="true"
                  className={`absolute top-4 right-1/2 h-0.5 w-[calc(100%+0.25rem)] ${done[STEPS[i - 1]] ? "bg-brand" : "bg-border"}`}
                />
              ) : null}
              <button
                type="button"
                aria-current={current ? "step" : undefined}
                aria-describedby={locked ? hintId : undefined}
                disabled={locked}
                onClick={() => onSelect(id)}
                className="group relative flex flex-col items-center gap-1 rounded-lg px-1 py-0.5 text-xs font-medium focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus disabled:cursor-not-allowed sm:text-sm"
              >
                <span
                  aria-hidden="true"
                  className={`flex size-8 items-center justify-center rounded-full text-sm font-semibold transition ${
                    reached ? "bg-brand text-brand-contrast" : "bg-surface-muted text-muted"
                  } ${current ? "ring-4 ring-brand-soft" : ""} ${locked ? "" : "group-hover:brightness-95"}`}
                >
                  {done[id] ? <Check aria-hidden="true" className="size-4" /> : i + 1}
                </span>
                <span className="sr-only">{`Step ${i + 1} `}</span>
                <span className={locked ? "text-muted" : "text-foreground"}>{LABELS[id]}</span>
                {done[id] ? <span className="sr-only">, done</span> : null}
              </button>
              {locked ? (
                <span id={hintId} className="text-[11px] leading-tight text-muted sm:text-xs">
                  {HINTS[id]}
                </span>
              ) : null}
            </li>
          );
        })}
      </ol>
    </nav>
  );
}
