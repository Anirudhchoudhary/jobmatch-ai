/** Match score as an SVG ring (0-100) with the number in the middle. */
export function ScoreRing({ score, size = 96 }: { score: number; size?: number }) {
  const value = Math.max(0, Math.min(100, Math.round(score)));
  const stroke = 9;
  const r = (size - stroke) / 2;
  const c = 2 * Math.PI * r;
  return (
    <div
      role="meter"
      aria-label="Match score"
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={value}
      aria-valuetext={`${value} out of 100`}
      className="relative shrink-0"
      style={{ width: size, height: size }}
    >
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} aria-hidden="true" className="-rotate-90">
        <defs>
          <linearGradient id="score-ring-gradient" x1="0" y1="0" x2="1" y2="1">
            <stop offset="0%" stopColor="var(--btn-from, #4f46e5)" />
            <stop offset="100%" stopColor="var(--brand-2, #06b6d4)" />
          </linearGradient>
        </defs>
        <circle cx={size / 2} cy={size / 2} r={r} fill="none" strokeWidth={stroke} className="stroke-border" />
        <circle
          cx={size / 2}
          cy={size / 2}
          r={r}
          fill="none"
          strokeWidth={stroke}
          strokeLinecap="round"
          stroke="url(#score-ring-gradient)"
          strokeDasharray={c}
          strokeDashoffset={c * (1 - value / 100)}
        />
      </svg>
      <span aria-hidden="true" className="absolute inset-0 flex items-center justify-center text-3xl font-semibold tabular-nums">
        {value}
      </span>
    </div>
  );
}
