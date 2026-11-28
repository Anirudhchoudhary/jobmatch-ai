import { Loader2 } from "lucide-react";
import type { ButtonHTMLAttributes } from "react";

type Variant = "primary" | "secondary" | "ghost" | "link";

const variants: Record<Variant, string> = {
  primary:
    "bg-gradient-to-br from-brand to-brand-2 text-white shadow-sm hover:brightness-110 active:brightness-95 px-4 py-2.5 font-medium",
  secondary:
    "border border-border bg-surface text-foreground shadow-sm hover:bg-surface-muted active:bg-border px-4 py-2.5 font-medium",
  ghost: "text-foreground hover:bg-surface-muted active:bg-border px-3 py-2 font-medium",
  link: "text-brand underline underline-offset-2 hover:text-brand-hover px-1 py-0.5",
};

export function Button({
  variant = "primary",
  loading = false,
  className = "",
  type = "button",
  disabled,
  children,
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant; loading?: boolean }) {
  return (
    <button
      type={type}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      className={`inline-flex items-center justify-center gap-2 rounded-lg text-sm transition focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus disabled:cursor-not-allowed disabled:opacity-60 ${variants[variant]} ${className}`}
      {...props}
    >
      {loading ? <Loader2 role="status" aria-label="Loading" className="jm-spin size-4" /> : null}
      {children}
    </button>
  );
}
