import type { ButtonHTMLAttributes } from "react";

type Variant = "primary" | "secondary" | "link";

const variants: Record<Variant, string> = {
  primary: "bg-brand text-brand-contrast hover:bg-brand-hover px-4 py-2.5 font-medium",
  secondary: "border border-border bg-surface text-foreground hover:bg-surface-muted px-4 py-2.5 font-medium",
  link: "text-brand underline underline-offset-2 hover:text-brand-hover px-1 py-0.5",
};

export function Button({
  variant = "primary",
  className = "",
  type = "button",
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant }) {
  return (
    <button
      type={type}
      className={`inline-flex items-center justify-center rounded-md text-sm transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus disabled:cursor-not-allowed disabled:opacity-60 ${variants[variant]} ${className}`}
      {...props}
    />
  );
}
