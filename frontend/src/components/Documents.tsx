"use client";

import { Check, FileText, Loader2, Sparkles, UploadCloud } from "lucide-react";
import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type ChangeEvent,
  type DragEvent,
} from "react";
import { Alert } from "@/components/ui/Alert";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { Progress, type ProgressStatus } from "@/components/Progress";
import { apiGet, GENERIC_ERROR_MESSAGE } from "@/lib/api";
import { streamSse } from "@/lib/sse";
import type { DocumentList, DocumentReady } from "@/lib/types";
import { uploadResume } from "@/lib/upload";

const JD_MIN = 50;
const JD_MAX = 20000;
/** How long "Both documents ready" shows before the flow moves on to Match. */
const CONFIRM_MS = 1000;

type DocType = "resume" | "jd";
type StoredDoc = DocumentList["documents"][number];

const messageOf = (e: unknown) =>
  e instanceof Error && e.message ? e.message : GENERIC_ERROR_MESSAGE;
const jdQualifies = (text: string) =>
  text.trim().length >= JD_MIN && text.trim().length <= JD_MAX;

/** `onReady(changed)`: both documents are present; `changed` is false when reporting the stored pair on mount. */
export function Documents({
  onReady,
}: {
  onReady: (changed: boolean) => void;
}) {
  const [docs, setDocs] = useState<StoredDoc[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [active, setActive] = useState<DocType[]>([]);
  const [ranTypes, setRanTypes] = useState<DocType[]>([]);
  const [replacing, setReplacing] = useState<DocType[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [held, setHeld] = useState(0);
  const [steps, setSteps] = useState<Record<string, ProgressStatus>>({});
  const [jd, setJd] = useState("");
  const [dragging, setDragging] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const notified = useRef(false);
  const inFlight = useRef(false);
  const mounted = useRef(true);
  const lastJd = useRef<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const confirmTimer = useRef<ReturnType<typeof setTimeout> | undefined>(
    undefined,
  );
  const onReadyRef = useRef(onReady);
  useEffect(() => {
    onReadyRef.current = onReady;
  });
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      clearTimeout(confirmTimer.current);
    };
  }, []);

  const busy = active.length > 0;

  const refresh = useCallback(async (changed = false) => {
    try {
      const { documents } = await apiGet<DocumentList>("/api/documents");
      if (!mounted.current) return;
      setDocs(documents);
      const both = ["resume", "jd"].every((t) =>
        documents.some((d) => d.doc_type === t),
      );
      // Fire once on first load, then again whenever a replacement succeeds.
      if (both && (changed || !notified.current)) {
        if (changed && !notified.current) {
          // The pair just became complete: confirm it for a moment before the flow moves on.
          setConfirming(true);
          clearTimeout(confirmTimer.current);
          confirmTimer.current = setTimeout(() => {
            if (!mounted.current) return;
            notified.current = true;
            setConfirming(false);
            onReadyRef.current(true);
          }, CONFIRM_MS);
        } else {
          notified.current = true;
          onReadyRef.current(changed);
        }
      }
    } catch (e) {
      if (mounted.current) setError(messageOf(e));
    } finally {
      if (mounted.current) setLoaded(true);
    }
  }, []);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void refresh();
  }, [refresh]);

  /** Runs one ingest; returns whether it succeeded. Only one runs at a time. */
  async function run(
    types: DocType[],
    task: () => Promise<void>,
  ): Promise<boolean> {
    if (inFlight.current) {
      setNotice("Please wait for the current document to finish.");
      return false;
    }
    inFlight.current = true;
    clearTimeout(confirmTimer.current);
    setConfirming(false);
    setActive(types);
    setRanTypes(types);
    setError(null);
    setNotice(null);
    setHeld(0);
    setSteps({});
    try {
      await task();
      setReplacing((r) => r.filter((t) => !types.includes(t)));
      await refresh(true);
      return true;
    } catch (e) {
      if (mounted.current) setError(messageOf(e));
      // A failed run may have cancelled the confirmation of a pair that is already stored.
      if (!notified.current) await refresh(true);
      return false;
    } finally {
      inFlight.current = false;
      if (mounted.current) setActive([]);
    }
  }

  function stream(path: string, body: unknown, doneCount = 1) {
    return streamSse(
      path,
      body,
      (name, data) => {
        if (name === "progress") {
          const p = data as { step: string; status: ProgressStatus };
          // A new document's pipeline starts over at validating (keeping the finished upload tick).
          setSteps((prev) =>
            p.step === "validating" && p.status === "started"
              ? {
                  ...(prev.uploading ? { uploading: prev.uploading } : {}),
                  [p.step]: p.status,
                }
              : { ...prev, [p.step]: p.status },
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

  const trySample = () =>
    void run(["resume", "jd"], () => stream("/api/samples", {}, 2));

  const submitJd = (text: string) => {
    const body = text.trim();
    if (body === lastJd.current) return; // already added; a failed attempt may still be retried
    void run(["jd"], () =>
      stream("/api/stream/documents", { doc_type: "jd", text: body }),
    ).then((ok) => {
      if (!ok) return;
      lastJd.current = body;
      if (mounted.current) setJd("");
    });
  };

  const submitResume = (file: File) =>
    void run(["resume"], async () => {
      setSteps({ uploading: "started" });
      const key = await uploadResume(file);
      setSteps({ uploading: "done" });
      await stream("/api/stream/documents", {
        doc_type: "resume",
        upload_key: key,
        filename: file.name,
      });
    });

  function onJdChange(e: ChangeEvent<HTMLTextAreaElement>) {
    const value = e.target.value;
    setJd(value);
    const fromPaste =
      (e.nativeEvent as InputEvent).inputType === "insertFromPaste";
    // Only a paste that leaves a qualifying text starts indexing; typed text waits for the button.
    if (fromPaste && jdQualifies(value)) submitJd(value);
  }

  function onDrop(e: DragEvent<HTMLLabelElement>) {
    e.preventDefault();
    setDragging(false);
    const file = e.dataTransfer?.files?.[0];
    if (file) submitResume(file); // run() ignores a drop while another ingest is in flight
  }

  const docOf = (t: DocType) => docs.find((d) => d.doc_type === t);
  const startReplace = (t: DocType) =>
    setReplacing((r) => (r.includes(t) ? r : [...r, t]));
  const cancelReplace = (t: DocType) =>
    setReplacing((r) => r.filter((x) => x !== t));
  const showSampleBanner = loaded && !docOf("resume") && !busy;

  function statusCard(t: DocType) {
    const stored = docOf(t);
    const working = active.includes(t);
    const name =
      t === "resume" ? (stored?.filename ?? "Resume") : "Job description";
    const showSteps = ranTypes[0] === t && Object.keys(steps).length > 0;
    return (
      <Card
        key={t}
        data-testid={`doc-card-${t}`}
        className="flex flex-col gap-3 !p-4"
      >
        <div className="flex items-center gap-3">
          <span className="grid size-9 shrink-0 place-items-center rounded-lg bg-brand-soft text-brand">
            {working ? (
              <Loader2 aria-hidden="true" className="jm-spin size-5" />
            ) : (
              <FileText aria-hidden="true" className="size-5" />
            )}
          </span>
          <div className="min-w-0 flex-1">
            <p className="truncate font-medium">{name}</p>
            <p className="text-sm text-muted">
              {t === "resume" ? "Resume" : "Added from pasted text"}
              {stored && !working
                ? ` · added ${new Date(stored.created_at).toLocaleString()}`
                : ""}
            </p>
          </div>
          <div aria-live="polite" className="flex items-center gap-2">
            {working ? (
              <Badge tone="brand">Working</Badge>
            ) : (
              <>
                <Badge tone="success">
                  <Check aria-hidden="true" className="mr-1 size-3" />
                  Ready
                </Badge>
                <Button
                  variant="ghost"
                  onClick={() => startReplace(t)}
                  aria-label={`Replace ${t === "resume" ? "resume" : "job description"}`}
                >
                  Replace
                </Button>
              </>
            )}
          </div>
        </div>
        <div aria-live="polite" className="flex flex-col gap-3">
          {showSteps ? <Progress steps={steps} /> : null}
        </div>
        {working && !showSteps && active.length > 1 ? (
          <p className="text-sm text-muted">
            Added together with the sample resume.
          </p>
        ) : null}
      </Card>
    );
  }

  const resumeSlot =
    active.includes("resume") ||
    (docOf("resume") && !replacing.includes("resume")) ? (
      statusCard("resume")
    ) : (
      <div className="flex flex-col gap-2">
        <label
          htmlFor="resume-file"
          data-dragging={dragging}
          onDragOver={(e) => {
            e.preventDefault();
            setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={onDrop}
          className={`flex cursor-pointer flex-col items-center justify-center gap-1.5 rounded-[var(--radius-card)] border-2 border-dashed px-4 py-8 text-center transition focus-within:outline-2 focus-within:outline-offset-2 focus-within:outline-focus ${
            dragging
              ? "border-brand bg-brand-soft"
              : "border-border bg-surface hover:bg-surface-muted"
          }`}
        >
          <UploadCloud aria-hidden="true" className="size-6 text-brand" />
          <span className="font-medium">Drop your resume PDF here</span>
          <span className="text-sm text-muted">
            or click to choose a file · PDF up to 5 MB
          </span>
          <input
            id="resume-file"
            type="file"
            accept="application/pdf"
            disabled={busy}
            className="sr-only"
            onChange={(e) => {
              const file = e.target.files?.[0];
              e.target.value = "";
              if (file) submitResume(file);
            }}
          />
        </label>
        {docOf("resume") ? (
          <div>
            <Button variant="link" onClick={() => cancelReplace("resume")}>
              Keep the current resume
            </Button>
          </div>
        ) : null}
      </div>
    );

  const jdSlot =
    active.includes("jd") || (docOf("jd") && !replacing.includes("jd")) ? (
      statusCard("jd")
    ) : (
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
          placeholder="Paste the job description here…"
          onChange={onJdChange}
          aria-describedby="jd-hint"
          className="w-full rounded-lg border border-border bg-surface px-3 py-2.5 text-base placeholder:text-muted focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-focus"
        />
        <p id="jd-hint" className="text-sm text-muted">
          Paste {JD_MIN} to {JD_MAX.toLocaleString("en-US")} characters and it
          is added at once, or type it and press the button.
        </p>
        <div className="flex gap-2">
          <Button
            onClick={() => submitJd(jd)}
            disabled={busy || !jdQualifies(jd)}
          >
            Add job description
          </Button>
          {docOf("jd") ? (
            <Button variant="ghost" onClick={() => cancelReplace("jd")}>
              Keep the current one
            </Button>
          ) : null}
        </div>
      </div>
    );

  return (
    <div className="flex flex-col gap-4">
      {showSampleBanner ? (
        <div className="flex flex-wrap items-center gap-3 rounded-[var(--radius-card)] border border-brand/30 bg-brand-soft px-4 py-3">
          <Sparkles aria-hidden="true" className="size-5 text-brand" />
          <p className="flex-1 text-sm">
            <b>No resume handy?</b> Use a sample resume and job description to
            see how it works.
          </p>
          <Button onClick={trySample}>Try the sample</Button>
        </div>
      ) : null}

      <div className="grid items-start gap-4 md:grid-cols-2">
        {resumeSlot}
        {jdSlot}
      </div>

      {confirming ? (
        <p
          role="status"
          className="flex items-center gap-2 text-sm font-medium text-success"
        >
          <Check aria-hidden="true" className="size-4" />
          Both documents ready
        </p>
      ) : null}
      {held > 0 ? (
        <Alert tone="info">
          Some content was held back because it looked unsafe. It will not be
          used in your analysis.
        </Alert>
      ) : null}
      {notice ? <Alert tone="info">{notice}</Alert> : null}
      {error ? <Alert>{error}</Alert> : null}
    </div>
  );
}
