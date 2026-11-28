"use client";

import Link from "next/link";
import { PlayCircle } from "lucide-react";
import { Workspace } from "@/components/Workspace";
import { buttonClasses } from "@/components/ui/Button";
import { Logo } from "@/components/ui/Logo";
import { BackendProvider } from "@/lib/backend";
import { createDemoBackend, type DemoRecording } from "@/lib/demoBackend";
import { useSession } from "@/lib/session";
import recording from "@/demo/recording.json";

// One replaying backend for the page's lifetime: no sign-in, no network, nothing spent.
const demoBackend = createDemoBackend(recording as unknown as DemoRecording);

/** The guided flow on a recorded run of the sample resume, for visitors who have not signed in. */
export default function DemoPage() {
  const { user } = useSession();
  const [cta, ctaHref] = user ? ["Open the app", "/app/"] : ["Sign in", "/"];
  return (
    <main className="mx-auto w-full max-w-4xl flex-1 px-4 py-6 sm:px-6">
      <header className="flex flex-wrap items-center justify-between gap-3 border-b border-border pb-4">
        <Link
          href="/"
          aria-label="JobMatch AI home"
          className="rounded-lg focus-visible:outline-2 focus-visible:outline-focus"
        >
          <Logo />
        </Link>
        <Link href={ctaHref} className={buttonClasses("secondary")}>
          {cta}
        </Link>
      </header>
      <div
        role="note"
        className="mt-4 flex flex-wrap items-center gap-3 rounded-[var(--radius-card)] border border-brand/30 bg-brand-soft px-4 py-3"
      >
        <PlayCircle aria-hidden="true" className="size-5 shrink-0 text-brand" />
        <p className="min-w-[14rem] flex-1 text-sm">
          <b>Recorded example with a sample resume.</b> Every step below is a
          real run, replayed in your browser.
        </p>
        <Link
          href={ctaHref}
          className={buttonClasses("link", "text-sm font-medium")}
        >
          {user ? "Run it on your own documents" : "Sign in to run it on yours"}{" "}
          <span aria-hidden="true">→</span>
        </Link>
      </div>
      <BackendProvider backend={demoBackend}>
        <Workspace landOn="match" />
      </BackendProvider>
    </main>
  );
}
