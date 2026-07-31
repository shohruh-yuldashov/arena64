import type { ReactNode } from "react";

/**
 * Reserved for the future authenticated app shell — play, profile,
 * friends, and so on (this task's "Do NOT implement" list). No page
 * exists under this group yet, so it is currently unreachable; that is
 * intentional scaffolding, not a bug. When the first authenticated route
 * is added, its chrome (likely a persistent nav distinct from the public
 * `SiteHeader`) belongs here, gated by `providers/auth-provider.tsx` once
 * that provider does more than return a placeholder.
 */
export default function AppShellLayout({ children }: { children: ReactNode }) {
  return <>{children}</>;
}
