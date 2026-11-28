import { useEffect, useRef, useState } from "react";

/** Frames over which a backlog is cleared: ~12 frames (~200 ms) per e-fold, so 2,000 chars land in ~1 s. */
const CATCH_UP_FRAMES = 12;
const TINY_BACKLOG = 3;

export function prefersReducedMotion(): boolean {
  if (typeof window === "undefined" || typeof window.matchMedia !== "function") return false;
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

/**
 * Reveals `target` a few characters per animation frame, speeding up in proportion to the backlog so
 * the text never lags the stream by much more than a second. `typing` stays true while the stream is
 * open (`done` false) or characters remain to be shown.
 */
export function useTypewriter(
  target: string,
  opts: { done: boolean; reducedMotion?: boolean },
): { shown: string; typing: boolean } {
  const { done } = opts;
  const reduced = opts.reducedMotion ?? prefersReducedMotion();
  const [count, setCount] = useState(0);
  const countRef = useRef(0);

  useEffect(() => {
    if (reduced) return;
    let raf = 0;
    const tick = () => {
      const backlog = target.length - countRef.current;
      if (backlog <= 0) return;
      countRef.current += Math.ceil(backlog / CATCH_UP_FRAMES);
      setCount(countRef.current);
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [target, reduced]);

  const backlog = target.length - Math.min(count, target.length);
  const showAll = reduced || (done && backlog <= TINY_BACKLOG);
  const shown = showAll ? target : target.slice(0, count);
  return { shown, typing: !done || shown.length < target.length };
}
