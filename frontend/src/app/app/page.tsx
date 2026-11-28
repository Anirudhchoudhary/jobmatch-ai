"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { Logo } from "@/components/ui/Logo";
import { Workspace } from "@/components/Workspace";
import { useSession } from "@/lib/session";

export default function AppPage() {
  const router = useRouter();
  const { user, loading, signOutError, signOut } = useSession();

  useEffect(() => {
    // Static export has no server redirects, so signed-out visitors are sent home on the client.
    if (!loading && !user) router.replace("/");
  }, [loading, user, router]);

  if (loading || !user) {
    return (
      <main className="mx-auto w-full max-w-4xl flex-1 px-4 py-10 sm:px-6">
        <p role="status" className="text-muted">
          Loading…
        </p>
      </main>
    );
  }

  return (
    <main className="mx-auto w-full max-w-4xl flex-1 px-4 py-6 sm:px-6">
      <header className="flex flex-wrap items-center justify-between gap-3 border-b border-border pb-4">
        <Logo />
        <div className="flex min-w-0 items-center gap-3 text-sm">
          <span className="truncate text-muted">{user.email}</span>
          <Button variant="secondary" onClick={() => void signOut()}>
            Sign out
          </Button>
        </div>
      </header>
      {signOutError ? (
        <div className="mt-4">
          <Alert>{signOutError}</Alert>
        </div>
      ) : null}
      <Workspace />
    </main>
  );
}
