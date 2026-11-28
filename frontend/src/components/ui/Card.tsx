import type { HTMLAttributes, ReactNode } from "react";

export function Card({
  className = "",
  title,
  actions,
  children,
  ...props
}: Omit<HTMLAttributes<HTMLDivElement>, "title"> & { title?: string; actions?: ReactNode }) {
  return (
    <div
      className={`rounded-[var(--radius-card)] border border-border bg-surface p-5 shadow-[var(--shadow-card)] sm:p-6 ${className}`}
      {...props}
    >
      {title || actions ? (
        <div className="mb-4 flex items-center justify-between gap-3">
          {title ? <h2 className="text-base font-semibold">{title}</h2> : null}
          {actions ? <div className={title ? "" : "ml-auto"}>{actions}</div> : null}
        </div>
      ) : null}
      {children}
    </div>
  );
}
