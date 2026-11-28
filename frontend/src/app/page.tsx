"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { FileCheck, MessageSquareQuote, ShieldCheck, Sparkles } from "lucide-react";
import { SignIn } from "@/components/SignIn";
import { Alert } from "@/components/ui/Alert";
import { Card } from "@/components/ui/Card";
import { Logo } from "@/components/ui/Logo";
import { useSession } from "@/lib/session";

const FEATURES = [
  { icon: FileCheck, text: "A match score with the resume lines behind every skill" },
  { icon: Sparkles, text: "Stronger bullet rewrites, grounded in what you wrote" },
  { icon: MessageSquareQuote, text: "A chat that cites your documents" },
  { icon: ShieldCheck, text: "Guardrails you can inspect" },
];

export default function Home() {
  const router = useRouter();
  const { user, notice } = useSession();

  useEffect(() => {
    if (user) router.replace("/app/");
  }, [user, router]);

  return (
    <div className="flex flex-1 flex-col">
      <header className="mx-auto w-full max-w-5xl px-4 py-5 sm:px-6">
        <Logo />
      </header>
      <main className="mx-auto flex w-full max-w-5xl flex-1 flex-col justify-center gap-10 px-4 pb-12 sm:px-6 md:flex-row md:items-center md:gap-14">
        <section className="flex-1">
          <h1 className="text-3xl font-bold leading-tight tracking-tight sm:text-4xl md:text-5xl">
            See how well your resume fits a job, with the evidence.
          </h1>
          <p className="mt-4 max-w-prose text-base text-muted sm:text-lg">
            Match score · resume rewrites · a chat that cites your documents · guardrails you can inspect.
          </p>
          <ul className="mt-8 flex flex-col gap-3 text-sm">
            {FEATURES.map(({ icon: Icon, text }) => (
              <li key={text} className="flex items-center gap-3">
                <span className="flex size-8 shrink-0 items-center justify-center rounded-lg bg-brand-soft text-brand">
                  <Icon aria-hidden="true" className="size-4" />
                </span>
                {text}
              </li>
            ))}
          </ul>
        </section>
        <Card className="w-full md:max-w-sm" title="Sign in with a code">
          <div className="flex flex-col gap-4">
            {notice ? <Alert>{notice}</Alert> : null}
            <SignIn />
          </div>
        </Card>
      </main>
    </div>
  );
}
