import { useId } from "react";

/** The JobMatch AI mark (same artwork as app/icon.svg) plus the product name. */
export function Logo({ size = 32 }: { size?: number }) {
  const gradientId = useId();
  return (
    <span className="inline-flex items-center gap-2.5">
      <svg width={size} height={size} viewBox="0 0 64 64" aria-hidden="true" focusable="false">
        <defs>
          <linearGradient id={gradientId} x1="0" y1="0" x2="1" y2="1">
            <stop offset="0" stopColor="#4f46e5" />
            <stop offset="1" stopColor="#06b6d4" />
          </linearGradient>
        </defs>
        <rect width="64" height="64" rx="16" fill={`url(#${gradientId})`} />
        <rect x="14" y="18" width="20" height="28" rx="4" fill="#fff" opacity=".95" />
        <rect x="18" y="24" width="12" height="2.5" rx="1.25" fill="#c7d2fe" />
        <rect x="18" y="29" width="9" height="2.5" rx="1.25" fill="#c7d2fe" />
        <circle cx="42" cy="38" r="10" fill="#fff" />
        <path
          d="M37.5 38.2l3.2 3.2 6-6.2"
          stroke="#4f46e5"
          strokeWidth="3"
          fill="none"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
      <span className="text-base font-semibold tracking-tight">JobMatch AI</span>
    </span>
  );
}
