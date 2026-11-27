import { useId, type InputHTMLAttributes } from "react";

export function Input({
  label,
  hint,
  className = "",
  id,
  ...props
}: InputHTMLAttributes<HTMLInputElement> & { label: string; hint?: string }) {
  const autoId = useId();
  const inputId = id ?? autoId;
  const hintId = `${inputId}-hint`;
  return (
    <div className="flex flex-col gap-1.5">
      <label htmlFor={inputId} className="text-sm font-medium">
        {label}
      </label>
      <input
        id={inputId}
        aria-describedby={hint ? hintId : undefined}
        className={`w-full rounded-md border border-border bg-surface px-3 py-2.5 text-base placeholder:text-muted focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-focus ${className}`}
        {...props}
      />
      {hint ? (
        <p id={hintId} className="text-sm text-muted">
          {hint}
        </p>
      ) : null}
    </div>
  );
}
