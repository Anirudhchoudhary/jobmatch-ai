import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { prefersReducedMotion, useTypewriter } from "./typewriter";

describe("useTypewriter", () => {
  beforeEach(() => vi.useFakeTimers({ toFake: ["requestAnimationFrame", "cancelAnimationFrame", "performance"] }));
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  const frames = (n: number) => act(() => void vi.advanceTimersByTime(n * 16));

  it("reveals text gradually, a few characters per frame", () => {
    const target = "x".repeat(30);
    const { result } = renderHook(() => useTypewriter(target, { done: false }));
    expect(result.current.shown).toBe("");
    frames(1);
    expect(result.current.shown.length).toBeGreaterThan(0);
    expect(result.current.shown.length).toBeLessThanOrEqual(3);
    expect(result.current.typing).toBe(true);
    frames(60);
    expect(result.current.shown).toBe(target);
  });

  it("keeps typing while the stream is open and stops once done and caught up", () => {
    const { result, rerender } = renderHook(({ t, d }) => useTypewriter(t, { done: d }), {
      initialProps: { t: "Hello", d: false },
    });
    frames(30);
    expect(result.current).toEqual({ shown: "Hello", typing: true });
    rerender({ t: "Hello", d: true });
    expect(result.current.typing).toBe(false);
  });

  it("clears a 2000-character backlog within about a second", () => {
    const target = "y".repeat(2000);
    const { result } = renderHook(() => useTypewriter(target, { done: false }));
    frames(75); // 1.2 s
    expect(result.current.shown).toBe(target);
  });

  it("keeps the shown text a prefix when the target grows mid-reveal", () => {
    const first = "a".repeat(40);
    const second = first + "b".repeat(40);
    const { result, rerender } = renderHook(({ t }) => useTypewriter(t, { done: false }), { initialProps: { t: first } });
    frames(2);
    const before = result.current.shown;
    expect(before.length).toBeGreaterThan(0);
    expect(before.length).toBeLessThan(40);
    rerender({ t: second });
    expect(second.startsWith(result.current.shown)).toBe(true);
    expect(result.current.shown.startsWith(before)).toBe(true);
    frames(60);
    expect(result.current.shown).toBe(second);
  });

  it("shows everything at once under reduced motion", () => {
    const { result } = renderHook(() => useTypewriter("all at once", { done: false, reducedMotion: true }));
    expect(result.current).toEqual({ shown: "all at once", typing: true });
  });

  it("is immediate when done and the backlog is tiny", () => {
    const { result } = renderHook(() => useTypewriter("abc", { done: true }));
    expect(result.current).toEqual({ shown: "abc", typing: false });
  });

  it("cancels its animation frame on unmount", () => {
    const cancel = vi.spyOn(window, "cancelAnimationFrame");
    const { unmount } = renderHook(() => useTypewriter("z".repeat(100), { done: false }));
    unmount();
    expect(cancel).toHaveBeenCalled();
  });
});

describe("prefersReducedMotion", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("is false when matchMedia is unavailable", () => {
    vi.stubGlobal("matchMedia", undefined);
    expect(prefersReducedMotion()).toBe(false);
  });

  it("reads the media query", () => {
    vi.stubGlobal("matchMedia", (q: string) => ({ matches: q.includes("reduce") }));
    expect(prefersReducedMotion()).toBe(true);
  });
});
