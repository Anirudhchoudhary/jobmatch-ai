import type { HTMLAttributes } from "react";

export function Card({ className = "", ...props }: HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={`rounded-lg border border-border bg-surface p-5 shadow-sm sm:p-6 ${className}`}
      {...props}
    />
  );
}
