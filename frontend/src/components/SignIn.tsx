"use client";

import { useState, type FormEvent } from "react";
import { useRouter } from "next/navigation";
import { ApiError, GENERIC_ERROR_MESSAGE, apiPost } from "@/lib/api";
import { useSession } from "@/lib/session";
import { Alert } from "./ui/Alert";
import { Button } from "./ui/Button";
import { Input } from "./ui/Input";

type Step = "email" | "code";

function messageOf(e: unknown): string {
  return e instanceof ApiError ? e.message : GENERIC_ERROR_MESSAGE;
}

export function SignIn() {
  const router = useRouter();
  const { refresh } = useSession();
  const [step, setStep] = useState<Step>("email");
  const [email, setEmail] = useState("");
  const [code, setCode] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function sendCode(): Promise<boolean> {
    setError(null);
    setBusy(true);
    try {
      await apiPost("/api/auth/request-code", { email: email.trim() });
      return true;
    } catch (e) {
      setError(messageOf(e));
      return false;
    } finally {
      setBusy(false);
    }
  }

  async function onEmail(ev: FormEvent) {
    ev.preventDefault();
    setInfo(null);
    // The same screen follows for new and existing addresses, so it reveals nothing about either.
    if (await sendCode()) setStep("code");
  }

  async function onResend() {
    setInfo(null);
    if (await sendCode()) setInfo("A new code was sent.");
  }

  async function onVerify(ev: FormEvent) {
    ev.preventDefault();
    setError(null);
    setInfo(null);
    setBusy(true);
    try {
      await apiPost("/api/auth/verify", { email: email.trim(), code: code.trim() });
      await refresh();
      router.push("/app/");
    } catch (e) {
      setError(messageOf(e));
      setBusy(false);
    }
  }

  if (step === "email") {
    return (
      <form onSubmit={onEmail} className="flex flex-col gap-4">
        <Input
          label="Email address"
          type="email"
          name="email"
          autoComplete="email"
          required
          maxLength={254}
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          hint="We email you a 6-digit sign-in code. No password."
        />
        {error ? <Alert>{error}</Alert> : null}
        <Button type="submit" disabled={busy}>
          {busy ? "Sending…" : "Send sign-in code"}
        </Button>
      </form>
    );
  }

  return (
    <form onSubmit={onVerify} className="flex flex-col gap-4">
      <Input
        label="6-digit code"
        name="code"
        inputMode="numeric"
        autoComplete="one-time-code"
        pattern="[0-9]{6}"
        maxLength={6}
        required
        autoFocus
        value={code}
        onChange={(e) => setCode(e.target.value.replace(/\D/g, ""))}
        hint="If that address can receive mail, a code is on its way. It expires shortly."
      />
      {error ? <Alert>{error}</Alert> : null}
      {info ? <Alert tone="info">{info}</Alert> : null}
      <Button type="submit" disabled={busy || code.length !== 6}>
        {busy ? "Checking…" : "Verify and continue"}
      </Button>
      <div className="flex flex-wrap items-center justify-between gap-2 text-sm">
        <Button variant="link" onClick={onResend} disabled={busy}>
          Resend code
        </Button>
        <Button
          variant="link"
          onClick={() => {
            setStep("email");
            setCode("");
            setError(null);
            setInfo(null);
          }}
        >
          Use a different email
        </Button>
      </div>
    </form>
  );
}
