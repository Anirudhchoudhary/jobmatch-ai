"use client";

import { createContext, useContext, type ReactNode } from "react";
import { apiGet } from "./api";
import { streamSse } from "./sse";
import { uploadResume } from "./upload";

export type StreamHandler = (name: string, data: unknown) => void;

/**
 * Where the workspace gets its data. The real backend talks to the API; the demo backend replays
 * a recorded run so the guided flow can be shown without signing in or spending anything.
 */
export interface Backend {
  /** True for the recorded demo: inputs that would create data are hidden. */
  demo: boolean;
  /** Starter questions for the chat (the chat's defaults when absent). */
  starters?: readonly string[];
  get<T>(path: string): Promise<T>;
  stream(path: string, body: unknown, onEvent: StreamHandler, signal?: AbortSignal, doneCount?: number): Promise<void>;
  upload(file: File): Promise<string>;
}

export const realBackend: Backend = {
  demo: false,
  // Delegating (not aliasing) keeps test spies on the api/sse/upload modules effective.
  get: <T,>(path: string) => apiGet<T>(path),
  stream: (path, body, onEvent, signal, doneCount) => streamSse(path, body, onEvent, signal, doneCount),
  upload: (file) => uploadResume(file),
};

const BackendContext = createContext<Backend>(realBackend);

export function BackendProvider({ backend, children }: { backend: Backend; children: ReactNode }) {
  return <BackendContext.Provider value={backend}>{children}</BackendContext.Provider>;
}

/** The current backend; the real API unless a `BackendProvider` says otherwise. */
export function useBackend(): Backend {
  return useContext(BackendContext);
}
