import { apiPost, ApiError, errorFromResponse } from "./api";
import type { UploadTicket } from "./types";

const MAX_BYTES = 5 * 1024 * 1024;

const isRelative = (url: string) => url.startsWith("/") && !url.startsWith("//");

/**
 * Validate the PDF, get an upload ticket and PUT the file with exactly the ticket's headers.
 * Returns the upload key. The local-dev PUT target is a relative API URL that needs the session
 * cookie; the absolute presigned storage URL never receives cookies.
 */
export async function uploadResume(file: File): Promise<string> {
  if (file.type !== "application/pdf") {
    throw new ApiError(0, "unsupported_media_type", "Upload a PDF file.");
  }
  if (file.size > MAX_BYTES) {
    throw new ApiError(0, "too_large", "That file is too large. The limit is 5 MB.");
  }
  const ticket = await apiPost<UploadTicket>("/api/uploads", {
    content_type: "application/pdf",
    size_bytes: file.size,
    filename: file.name,
  });
  const res = await fetch(ticket.url, {
    method: ticket.method,
    headers: ticket.headers,
    body: file,
    credentials: isRelative(ticket.url) ? "same-origin" : "omit",
  });
  if (!res.ok) throw await errorFromResponse(res);
  return ticket.key;
}
