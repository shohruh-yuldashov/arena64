import type { ReactNode } from "react";

import { isResolved } from "@/entities/session";
import { useSession } from "@/features/auth/model/session-provider";
import { useNotificationPush } from "@/features/notifications/model/use-notification-push";
import { MatchOfferSurface } from "@/widgets/match-offer";
import { NotificationBell } from "@/widgets/notification-bell";
import { PwaNotices } from "@/widgets/pwa";
import { SessionMenu } from "@/widgets/session-menu";
import { ThemeToggle } from "@/widgets/theme-toggle";

/**
 * The frame every page renders inside — header, main region, footer.
 *
 * ## Landmarks, and why they are not `div`s
 *
 * `<header>`, `<main>` and `<footer>` are navigation landmarks. A screen
 * reader user jumps between them directly; without them the whole page is
 * one undifferentiated region and the only way through it is linear. This
 * costs nothing and is the single highest-value accessibility decision in
 * a layout.
 *
 * ## The skip link
 *
 * First focusable element on the page, visually hidden until focused. A
 * keyboard user landing on any page can reach the content in one `Tab`
 * instead of traversing the whole header — WCAG 2.1 §2.4.1, and the one
 * thing a layout must provide because no component below it can.
 *
 * ## No business content
 *
 * No navigation to features that do not exist and no locale switcher —
 * each belongs to the phase that ships what it links to, and a header full
 * of dead links is worse than a bare one.
 *
 * `SessionMenu` is the exception, and it earns it: sign-in and sign-out are
 * what A64-020.2 builds, and a sign-out action that no screen exposes is
 * the "implemented and reachable from nothing" failure the backend audits
 * kept finding.
 */
export function AppShell({ children }: { children: ReactNode }) {
  // A64-021.2 §4. The one notification subscription, mounted here because
  // the shell is the only thing on every route — a `notification.created`
  // frame must refresh the badge whether the player is on `/play`, in a
  // game or reading a profile.
  //
  // Here rather than in `NotificationBell` so the subscription does not
  // depend on that widget continuing to be rendered, and rather than in a
  // provider because there is nothing to provide: it subscribes to the one
  // socket `app/providers` already owns and invalidates two query keys.
  useNotificationPush();

  // A64-022.6 §13. The pending match offer, on every authenticated page.
  //
  // Rendered conditionally rather than gated inside, so a signed-out
  // visitor mounts no query and makes no request — and so the hooks the
  // surface owns are unconditional relative to its own mount.
  //
  // Here for the reason `useNotificationPush` is: a match a player has
  // agreed to must reach them wherever they are, and until this existed
  // it reached them only on `/play`. A challenge accepted while they read
  // a profile had a ten-minute join window and nothing on the page said
  // so.
  const { state: session } = useSession();
  const signedIn = isResolved(session) && session.status === "authenticated";

  return (
    <div className="bg-background text-foreground flex min-h-full flex-col">
      <a
        href="#main"
        className="bg-background focus-visible:ring-ring sr-only rounded-md px-4 py-2 text-sm font-medium focus-visible:not-sr-only focus-visible:absolute focus-visible:top-2 focus-visible:left-2 focus-visible:z-50 focus-visible:ring-2"
      >
        Skip to content
      </a>

      <header className="border-b">
        <div className="mx-auto flex h-14 max-w-5xl items-center justify-between px-4">
          <span className="text-sm font-semibold tracking-tight">Arena64</span>
          <div className="flex items-center gap-2">
            <SessionMenu />
            {/* A64-021.1. The one entry point to `/notifications`, and the
                unread badge. Beside the session menu rather than inside it,
                because a badge a player has to open a menu to see is a badge
                that tells them nothing. Renders nothing when signed out. */}
            <NotificationBell />
            <ThemeToggle />
          </div>
        </div>
      </header>

      <main id="main" tabIndex={-1} className="mx-auto w-full max-w-5xl flex-1 px-4 py-8">
        {children}
      </main>

      <footer className="border-t">
        <div className="text-muted-foreground mx-auto flex h-12 max-w-5xl items-center px-4 text-xs">
          Arena64
        </div>
      </footer>

      {/* A64-020.9. Offline, update and install, on every page — the layout
          is the only place all three can live, because all three outlive
          the route the player happens to be on. Last in the DOM and pinned
          by its own positioning, so it is last in the tab order and covers
          nothing until it has something to say. */}
      {signedIn && <MatchOfferSurface />}

      <PwaNotices />
    </div>
  );
}
