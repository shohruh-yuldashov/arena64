"use client";

import { createContext, use, useMemo, type ReactNode } from "react";

/**
 * Placeholder only — this task explicitly excludes `auth` (no login page,
 * no session, no token). What exists here is the *shape* every future
 * consumer will code against: `useAuth()` returning a status and a user.
 * When a real `auth` module exists, only this file's internals change —
 * fetching a session, subscribing to sign-in/sign-out — and no caller of
 * `useAuth()` elsewhere in the app needs to.
 */
interface AuthContextValue {
  status: "unauthenticated";
  user: null;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const value = useMemo<AuthContextValue>(
    () => ({ status: "unauthenticated", user: null }),
    [],
  );

  return <AuthContext value={value}>{children}</AuthContext>;
}

export function useAuth(): AuthContextValue {
  const context = use(AuthContext);
  if (!context) {
    throw new Error("useAuth must be used within an AuthProvider");
  }
  return context;
}
