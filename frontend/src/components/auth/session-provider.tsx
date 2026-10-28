"use client";

import { useLocale } from "next-intl";
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { hardNavigate } from "@/lib/navigate";
import { ApiProblem, getSession, logout, onUnauthorized, type Session } from "@/lib/api";

type State =
  | { status: "loading" }
  | { status: "anonymous" }
  | { status: "error" } // the backend could not be asked (not the same as "signed out")
  | { status: "authenticated"; session: Session };

type SessionContext = {
  state: State;
  /** Remember a session that was just created (after sign-in) without asking again. */
  setSession: (session: Session) => void;
  /** Ends the session; resolves to false when that failed and the person is still signed in. */
  signOut: () => Promise<boolean>;
};

const Context = createContext<SessionContext | null>(null);

export function SessionProvider({ children }: { children: ReactNode }) {
  const locale = useLocale();
  const [state, setState] = useState<State>({ status: "loading" });

  useEffect(() => {
    let current = true;
    getSession().then(
      (session) => {
        if (current)
          setState(session ? { status: "authenticated", session } : { status: "anonymous" });
      },
      () => {
        if (current) setState({ status: "error" });
      },
    );
    // Any later call answered with 401 means the session ended (expired, revoked, role change).
    const stop = onUnauthorized(() => setState({ status: "anonymous" }));
    return () => {
      current = false;
      stop();
    };
  }, []);

  const setSession = useCallback((session: Session) => {
    setState({ status: "authenticated", session });
  }, []);

  const signOut = useCallback(async () => {
    if (state.status !== "authenticated") return true;
    try {
      const providerUrl = await logout(state.session.csrf_token);
      // A full navigation: nothing of the signed-in page stays in memory. The state is left as
      // it is on purpose: turning it to "anonymous" here would make the gate start a soft
      // navigation to /login that could beat the load of the provider's sign-out page, and the
      // provider's own session (a shared computer!) would survive.
      hardNavigate(providerUrl ?? `/${locale}/login`);
      return true;
    } catch (error) {
      // A 401 means the session was already gone; anything else leaves the person signed in.
      if (error instanceof ApiProblem && error.status === 401) {
        hardNavigate(`/${locale}/login`);
        return true;
      }
      return false;
    }
  }, [state, locale]);

  const value = useMemo(() => ({ state, setSession, signOut }), [state, setSession, signOut]);
  return <Context.Provider value={value}>{children}</Context.Provider>;
}

export function useSession(): SessionContext {
  const value = useContext(Context);
  if (!value) throw new Error("useSession must be used inside SessionProvider");
  return value;
}
