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

      <main id="public-main" tabIndex={-1} className="flex-1">
        {children}
      </main>

      <MarketingFooter onLanding={onLanding} />
    </div>
  );
}
