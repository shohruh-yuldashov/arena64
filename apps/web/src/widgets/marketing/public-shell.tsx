import { useRouterState } from "@tanstack/react-router";
import type { ReactNode } from "react";

import { useTranslation } from "@/shared/i18n";
import { MarketingFooter } from "@/widgets/marketing/footer";
import { MarketingHeader } from "@/widgets/marketing/header";

/**
 * The chrome for a page a visitor without an account can read —
 * A64-026.4 §43.5.
 *
 * It was the landing page's own frame, and stayed inside it while the
 * landing page was the only public page there was. `/tournaments` is now
 * the second, so the frame moved here — one definition, rather than a
 * second header that drifts from the first.
 *
 * ## Why not `AppShell`
 *
 * `AppShell`'s header is *product* navigation: Play, Tournaments, Friends,
 * Match history, all behind `protectedPage`. It collapses correctly for an
 * anonymous viewer — to a wordmark and "Sign in" — but that leaves a person
 * reading a shared tournament link with no way anywhere except an account
 * form. This shell gives them the page's own context and a footer that
 * leads somewhere.
 *
 * ## Two different `<main>`s, and the landing page is the odd one
 *
 * The landing page is full-bleed: each of its sections sets its own
 * max-width and its own vertical rhythm, so a container around them would
 * inset the borders that are meant to run edge to edge.
 *
 * Every other page here is a product page, written for `AppShell` and
 * expecting the container `AppShell` provides. Without it a heading sits
 * flush against the left edge of a phone screen — which is what the first
 * capture of `/tournaments` at 360 showed. So the non-landing case uses
 * `AppShell`'s own container classes: this is the same page in different
 * chrome, not a different page.
 *
 * ## The skip link is here, not in either page
 *
 * It has to be the first focusable element in the document, which is a fact
 * about the frame rather than about the content — a page cannot guarantee
 * it while a header is rendered above it.
 */
export function PublicShell({ children }: { children: ReactNode }) {
  const { t } = useTranslation();
  const pathname = useRouterState({ select: (router) => router.location.pathname });
  const onLanding = pathname === "/";

  return (
    <div className="bg-background text-foreground flex min-h-full flex-col">
      <a
        href="#public-main"
        className="bg-background focus-visible:ring-ring sr-only rounded-md px-4 py-2 text-sm font-medium focus-visible:not-sr-only focus-visible:absolute focus-visible:top-2 focus-visible:left-2 focus-visible:z-50 focus-visible:ring-2"
      >
        {t("layout.skipToContent")}
      </a>

      <MarketingHeader />

      {/* Literal class strings on both branches — Tailwind v4 scans source
          text and generates nothing for a name assembled at runtime. */}
      <main
        id="public-main"
        tabIndex={-1}
        className={onLanding ? "flex-1" : "mx-auto w-full max-w-5xl flex-1 px-4 py-8"}
      >
        {children}
      </main>

      <MarketingFooter onLanding={onLanding} />
    </div>
  );
}
