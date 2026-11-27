"use client";

import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { apiGet, apiPost, setUnauthorizedHandler } from "./api";

export const SESSION_EXPIRED_MESSAGE = "Your session expired. Sign in again.";
export const SIGN_OUT_FAILED_MESSAGE = "Sign-out failed. Please try again.";

export interface SessionUser {
  user_id: string;
  email: string;
}

interface SessionValue {
  user: SessionUser | null;
  loading: boolean;
  /** One-off message for the sign-in page, e.g. after the session expired. */
  notice: string | null;
  /** Set when the last sign-out failed; the user is still signed in. */
  signOutError: string | null;
  /** Reload the current user; resolves true when signed in. Never rejects. */
  refresh(): Promise<boolean>;
  /** Never rejects; on failure the user stays signed in and `signOutError` is set. */
  signOut(): Promise<void>;
}

const SessionContext = createContext<SessionValue | null>(null);

// The session lives only in the HttpOnly cookie; nothing about it is kept in browser storage.
export function SessionProvider({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const [user, setUser] = useState<SessionUser | null>(null);
  const [loading, setLoading] = useState(true);
  const [notice, setNotice] = useState<string | null>(null);
  const [signOutError, setSignOutError] = useState<string | null>(null);
  const userRef = useRef<SessionUser | null>(null);

  const apply = useCallback((next: SessionUser | null) => {
    userRef.current = next;
    setUser(next);
  }, []);

  const refresh = useCallback(async () => {
    try {
      apply(await apiGet<SessionUser>("/api/auth/me"));
      setNotice(null);
      return true;
    } catch {
      apply(null); // not signed in (or unreachable): treated as signed out
      return false;
    } finally {
      setLoading(false);
    }
  }, [apply]);

  const signOut = useCallback(async () => {
    setSignOutError(null);
    try {
      await apiPost("/api/auth/logout");
    } catch {
      // The cookie may still be valid, so stay signed in. A 401 has already signed us out.
      if (userRef.current !== null) setSignOutError(SIGN_OUT_FAILED_MESSAGE);
      return;
    }
    apply(null);
    setNotice(null);
    router.push("/");
  }, [apply, router]);

  useEffect(() => {
    setUnauthorizedHandler(() => {
      if (userRef.current === null) return; // already signed out: nothing expired
      apply(null);
      setNotice(SESSION_EXPIRED_MESSAGE);
      router.push("/");
    });
    return () => setUnauthorizedHandler(null);
  }, [apply, router]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- initial load of the session
    void refresh();
  }, [refresh]);

  const value = useMemo(
    () => ({ user, loading, notice, signOutError, refresh, signOut }),
    [user, loading, notice, signOutError, refresh, signOut],
  );
  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
}

export function useSession(): SessionValue {
  const ctx = useContext(SessionContext);
  if (!ctx) throw new Error("useSession must be used inside SessionProvider");
  return ctx;
}
