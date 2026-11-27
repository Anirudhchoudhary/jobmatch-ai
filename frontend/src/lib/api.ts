import type { ApiErrorBody } from "./types";

export const GENERIC_ERROR_MESSAGE = "Something went wrong. Please try again.";

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly requestId: string | undefined;

  constructor(status: number, code: string, message: string, requestId?: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.requestId = requestId;
  }
}

let onUnauthorized: (() => void) | null = null;

/** Register the handler called when the API answers 401 `auth_required`. */
export function setUnauthorizedHandler(handler: (() => void) | null): void {
  onUnauthorized = handler;
}

/** Build an ApiError from a non-2xx response, notifying the 401 handler. */
export async function errorFromResponse(res: Response): Promise<ApiError> {
  let body: Partial<ApiErrorBody> | null = null;
  try {
    body = (await res.json()) as Partial<ApiErrorBody>;
  } catch {
    body = null;
  }
  const detail = body?.error;
  const err = new ApiError(
    res.status,
    typeof detail?.code === "string" ? detail.code : "http_error",
    typeof detail?.message === "string" && detail.message ? detail.message : GENERIC_ERROR_MESSAGE,
    typeof detail?.request_id === "string" ? detail.request_id : undefined,
  );
  if (res.status === 401 && err.code === "auth_required") onUnauthorized?.();
  return err;
}

async function request<T>(path: string, init: RequestInit): Promise<T> {
  const res = await fetch(path, { ...init, credentials: "same-origin" });
  if (!res.ok) throw await errorFromResponse(res);
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export function apiGet<T>(path: string): Promise<T> {
  return request<T>(path, { method: "GET" });
}

export function apiPost<T>(path: string, body?: unknown): Promise<T> {
  return request<T>(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body ?? {}),
  });
}
