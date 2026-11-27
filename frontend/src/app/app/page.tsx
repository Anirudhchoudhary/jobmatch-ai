"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { useSession } from "@/lib/session";

export default function Workspace() {
  const router = useRouter();
  const { user, loading, signOut } = useSession();

  useEffect(() => {
    // Static export has no server redirects, so signed-out visitors are sent home on the client.
    if (!loading && !user) router.replace("/");
  }, [loading, user, router]);

  if (loading || !user) {
    return (
      <main className="mx-auto w-full max-w-5xl flex-1 px-4 py-10 sm:px-6">
        <p role="status" className="text-muted">
          Loading…
        </p>
      </main>
    );
  }

  return (
    <main className="mx-auto w-full max-w-5xl flex-1 px-4 py-6 sm:px-6">
      <header className="flex flex-wrap items-center justify-between gap-3 border-b border-border pb-4">
        <h1 className="text-xl font-semibold">JobMatch AI</h1>
        <div className="flex items-center gap-3 text-sm">
          <span className="text-muted">{user.email}</span>
          <Button variant="secondary" onClick={() => void signOut()}>
            Sign out
          </Button>
        </div>
      </header>
      <Card className="mt-6">
        <p className="text-muted">Your workspace is ready. Document upload and analysis arrive next.</p>
      </Card>
    </main>
  );
}
