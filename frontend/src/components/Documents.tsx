"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { Progress, type ProgressStatus } from "@/components/Progress";
import { apiGet, GENERIC_ERROR_MESSAGE } from "@/lib/api";
import { streamSse } from "@/lib/sse";
import type { DocumentList, DocumentReady } from "@/lib/types";
import { uploadResume } from "@/lib/upload";

const JD_MIN = 50;
const JD_MAX = 20000;

const messageOf = (e: unknown) => (e instanceof Error && e.message ? e.message : GENERIC_ERROR_MESSAGE);

/** `onReady(changed)`: both documents are present; `changed` is false when reporting the stored pair on mount. */
export function Documents({ onReady }: { onReady: (changed: boolean) => void }) {
  const [docs, setDocs] = useState<DocumentList["documents"]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [held, setHeld] = useState(0);
  const [steps, setSteps] = useState<Record<string, ProgressStatus>>({});
  const [jd, setJd] = useState("");
  const notified = useRef(false);
  const onReadyRef = useRef(onReady);
  useEffect(() => {
    onReadyRef.current = onReady;
  });

  const refresh = useCallback(async (changed = false) => {
    try {
      const { documents } = await apiGet<DocumentList>("/api/documents");
      setDocs(documents);
      const both = ["resume", "jd"].every((t) => documents.some((d) => d.doc_type === t));
      // Fire once on first load, then again whenever a replacement succeeds.
      if (both && (changed || !notified.current)) {
        notified.current = true;
        onReadyRef.current(changed);
      }
    } catch (e) {
      setError(messageOf(e));
    }
  }, []);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void refresh();
  }, [refresh]);

  async function run(task: () => Promise<void>) {
    setBusy(true);
    setError(null);
    setHeld(0);
    setSteps({});
    try {
      await task();
      await refresh(true);
    } catch (e) {
      setError(messageOf(e));
    } finally {
      setBusy(false);
    }
  }

  function stream(path: string, body: unknown, doneCount = 1) {
    return streamSse(
      path,
      body,
      (name, data) => {
        if (name === "progress") {
          const p = data as { step: string; status: ProgressStatus };
          // A new document's pipeline starts over at validating.
          setSteps((prev) =>
            p.step === "validating" && p.status === "started" ? { [p.step]: p.status } : { ...prev, [p.step]: p.status },
          );
        } else if (name === "done") {
          const d = data as DocumentReady;
          if (d.quarantined_count > 0) setHeld((n) => n + d.quarantined_count);
        }
      },
      undefined,
      doneCount,
    );
  }

  const trySample = () => run(() => stream("/api/samples", {}, 2));
  const submitJd = () => run(() => stream("/api/stream/documents", { doc_type: "jd", text: jd.trim() }).then(() => setJd("")));
  const submitResume = (file: File) =>
    run(async () => {
      const key = await uploadResume(file);
      await stream("/api/stream/documents", { doc_type: "resume", upload_key: key, filename: file.name });
    });

  const jdLength = jd.trim().length;
  const jdValid = jdLength >= JD_MIN && jdLength <= JD_MAX;

  return (
    <div className="mt-6 flex flex-col gap-4">
      <Card>
        <h2 className="text-lg font-semibold">Your documents</h2>
        {docs.length === 0 ? (
          <p className="mt-2 text-muted">No documents yet.</p>
        ) : (
          <ul className="mt-2 flex flex-col gap-1 text-sm">
            {docs.map((d) => (
              <li key={d.doc_id}>
                <span className="font-medium">{d.doc_type === "resume" ? "Resume" : "Job description"}</span>
                {d.filename ? ` · ${d.filename}` : ""}
                <span className="text-muted"> · added {new Date(d.created_at).toLocaleString()}</span>
              </li>
            ))}
          </ul>
        )}
        <div className="mt-4">
          <Button onClick={() => void trySample()} disabled={busy}>
            Try with sample resume + JD
          </Button>
        </div>
      </Card>

      <Card className="flex flex-col gap-4">
        <div className="flex flex-col gap-1.5">
          <label htmlFor="resume-file" className="text-sm font-medium">
            Resume (PDF, up to 5 MB)
          </label>
          <input
            id="resume-file"
            type="file"
            accept="application/pdf"
            disabled={busy}
            className="text-sm"
            onChange={(e) => {
              const file = e.target.files?.[0];
              e.target.value = "";
              if (file) void submitResume(file);
            }}
          />
        </div>
        <div className="flex flex-col gap-1.5">
          <label htmlFor="jd-text" className="text-sm font-medium">
            Job description
          </label>
          <textarea
            id="jd-text"
            rows={8}
            value={jd}
            maxLength={JD_MAX}
            disabled={busy}
            onChange={(e) => setJd(e.target.value)}
            aria-describedby="jd-hint"
            className="w-full rounded-md border border-border bg-surface px-3 py-2.5 text-base placeholder:text-muted focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-focus"
          />
          <p id="jd-hint" className="text-sm text-muted">
            Paste {JD_MIN} to {JD_MAX.toLocaleString("en-US")} characters.
          </p>
          <div>
            <Button onClick={() => void submitJd()} disabled={busy || !jdValid}>
              Use this job description
            </Button>
          </div>
        </div>
      </Card>

      {Object.keys(steps).length > 0 ? <Progress steps={steps} /> : null}
      {held > 0 ? (
        <Alert tone="info">
          Some content was held back because it looked unsafe. It will not be used in your analysis.
        </Alert>
      ) : null}
      {error ? <Alert>{error}</Alert> : null}
    </div>
  );
}
