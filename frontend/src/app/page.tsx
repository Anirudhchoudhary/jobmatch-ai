"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { SignIn } from "@/components/SignIn";
import { Alert } from "@/components/ui/Alert";
import { Card } from "@/components/ui/Card";
import { useSession } from "@/lib/session";

export default function Home() {
  const router = useRouter();
  const { user, notice } = useSession();

  useEffect(() => {
    if (user) router.replace("/app/");
  }, [user, router]);

  return (
    <main className="mx-auto flex w-full max-w-5xl flex-1 flex-col justify-center gap-8 px-4 py-10 sm:px-6 md:flex-row md:items-center md:gap-12">
      <section className="flex-1">
        <p className="text-sm font-semibold uppercase tracking-wide text-brand">JobMatch AI</p>
        <h1 className="mt-2 text-3xl font-semibold leading-tight sm:text-4xl">
          Match a resume to a job description, with the evidence shown.
        </h1>
        <p className="mt-4 max-w-prose text-base text-muted">
          Upload a resume and a job description, or try the sample. JobMatch AI scores the fit,
          cites the exact resume lines behind every skill, suggests stronger bullets and answers
          questions in a chat that has to back its claims. Its guardrails are visible: you can see
          what was checked, redacted or flagged.
        </p>
      </section>
      <Card className="w-full md:max-w-sm">
        <h2 className="text-lg font-semibold">Sign in</h2>
        <div className="mt-4 flex flex-col gap-4">
          {notice ? <Alert>{notice}</Alert> : null}
          <SignIn />
        </div>
      </Card>
    </main>
  );
}
