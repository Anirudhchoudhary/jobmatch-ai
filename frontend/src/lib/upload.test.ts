import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { uploadResume } from "./upload";

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

const pdf = (size = 10, type = "application/pdf") =>
  new File([new Uint8Array(size)], "cv.pdf", { type });

describe("uploadResume", () => {
  const fetchMock = vi.fn();
  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
  });
  afterEach(() => vi.unstubAllGlobals());

  it("rejects a non-PDF before any request", async () => {
    await expect(uploadResume(pdf(10, "text/plain"))).rejects.toThrow("Upload a PDF file.");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("rejects a 6 MB file before any request", async () => {
    await expect(uploadResume(pdf(6 * 1024 * 1024))).rejects.toThrow(
      "That file is too large. The limit is 5 MB.",
    );
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("PUTs to an absolute presigned URL with exactly the ticket headers and no cookies", async () => {
    const headers = { "Content-Type": "application/pdf", "x-amz-server-side-encryption": "AES256" };
    fetchMock
      .mockResolvedValueOnce(
        json(200, { key: "k1", url: "https://s3.example/bucket/k1?sig=1", method: "PUT", headers, max_bytes: 5242880 }),
      )
      .mockResolvedValueOnce(new Response(null, { status: 200 }));
    const file = pdf();
    await expect(uploadResume(file)).resolves.toBe("k1");
    const [ticketPath, ticketInit] = fetchMock.mock.calls[0];
    expect(ticketPath).toBe("/api/uploads");
    expect(JSON.parse(ticketInit.body)).toEqual({
      content_type: "application/pdf",
      size_bytes: 10,
      filename: "cv.pdf",
    });
    const [url, init] = fetchMock.mock.calls[1];
    expect(url).toBe("https://s3.example/bucket/k1?sig=1");
    expect(init.method).toBe("PUT");
    expect(init.headers).toEqual(headers);
    expect(init.body).toBe(file);
    expect(init.credentials).toBe("omit");
  });

  it("keeps the session cookie for the relative local PUT URL", async () => {
    fetchMock
      .mockResolvedValueOnce(
        json(200, { key: "k2", url: "/api/uploads/k2", method: "PUT", headers: { "Content-Type": "application/pdf" }, max_bytes: 1 }),
      )
      .mockResolvedValueOnce(new Response(null, { status: 204 }));
    await uploadResume(pdf());
    const [url, init] = fetchMock.mock.calls[1];
    expect(url).toBe("/api/uploads/k2");
    expect(init.credentials).toBe("same-origin");
  });

  it("throws when the storage PUT fails", async () => {
    fetchMock
      .mockResolvedValueOnce(
        json(200, { key: "k3", url: "https://s3.example/k3", method: "PUT", headers: {}, max_bytes: 1 }),
      )
      .mockResolvedValueOnce(new Response("<Error/>", { status: 403 }));
    await expect(uploadResume(pdf())).rejects.toThrow("Something went wrong. Please try again.");
  });
});
