import { Link, useRouterState } from "@tanstack/react-router";
import { MenuIcon } from "lucide-react";
import { useEffect, useState } from "react";

import { useSession } from "@/features/auth/model/session-provider";
import { useTranslation } from "@/shared/i18n";
import { cn } from "@/shared/lib/cn";
import { Button } from "@/shared/ui";
import { BrowserSettingsMenu } from "@/widgets/account-menu";
import { Brand } from "@/widgets/brand";

/**
 * The header a visitor without an account sees — A64-026.1 §40.2.
 *
 * ## Why the app shell's header is not reused
 *
 * `AppShell`'s header is a *product* navigation: Play, Tournaments,
 * Friends, Match history — every one of them behind `protectedPage`. For an
 * anonymous visitor it correctly collapses to a wordmark, a theme toggle
 * and "Sign in", which is right for a person who took a wrong turn into
 * `/play` and wrong for one who has just arrived at the front door.
 *
 * This replaces it on the landing page only. The shell keeps its header
 * everywhere else, and nothing about the signed-in product changes.
 *
 * ## The navigation points at sections, not at routes
 *
 * `/play` and `/friends` are behind a guard, so a link to either in this
 * header would be a link that lies about where it goes — the defect
 * A64-025.3 §2 refused to ship on the home page. The links are in-page
 * anchors to the sections that explain those features instead: a visitor
 * gets taken to the explanation, and the CTA takes them to the account that
 * unlocks the thing itself.
 *
 * ## Off the landing page, an anchor points at nothing — A64-026.4 §43.5
 *
 * This header is no longer the landing page's alone. `/tournaments` opened
 * to visitors without an account, and an anonymous visitor there needs
 * chrome that is not the product shell's guarded navigation.
 *
 * `#play` on a page with no `#play` scrolls nowhere, so the anchors are
 * rendered **only where their sections exist**. Everywhere else the nav is
 * the one destination that is a real route, and the wordmark goes home. A
 * shorter nav is the correct one; a nav of anchors to absent sections is
 * the same lying link in a different costume.
 *
 * ## Sticky, and it earns it
 *
 * The page is long enough that the call to action leaves the viewport, and
 * a landing page whose primary action is only reachable by scrolling back
 * up is one that measures conversion badly. It is `h-14`, the same height
 * the product header uses, so the two do not jump when a visitor signs in.
 *
 * The border appears only once the page has moved: a hairline under a
 * header that is flush with the top of the document is a line for nothing.
 */
export function MarketingHeader() {
  const { t } = useTranslation();
  const { state } = useSession();
  const pathname = useRouterState({ select: (router) => router.location.pathname });
  const [scrolled, setScrolled] = useState(false);

  // The sections this header's anchors point at live on `/` and nowhere
  // else. Read from the router rather than passed in as a prop, because a
  // caller could pass the wrong one and the router cannot.
  const onLanding = pathname === "/";

  // The auth links appear only for `anonymous`, which is the one state that
  // actually means "there is no session" — A64-025.9B's rule, enforced by
  // `auth.test.tsx`. Offering "Sign in" while the session is merely
  // `unavailable` tells a signed-in player they are signed out because one
  // request failed, which is the exact claim that state exists to avoid;
  // offering it while `bootstrapping` is a flicker.
  const offerAuth = state.status === "anonymous";
  const [menuOpen, setMenuOpen] = useState(false);

  useEffect(() => {
    const onScroll = () => setScrolled(window.scrollY > 8);
    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, []);

  return (
    <header
      className={cn(
        "bg-background/85 sticky top-0 z-40 backdrop-blur transition-colors duration-fast",
        scrolled ? "border-border border-b" : "border-b border-transparent",
      )}
    >
      <div className="mx-auto flex h-14 max-w-6xl items-center gap-3 px-4">
        {/* A64-026.2 §41.1. The shared wordmark, a step larger here because
            this header is the first thing a visitor sees and the product's
            is not. One definition, three sizes. */}
        <Brand size="base" />

        <nav aria-label={t("landing.nav.label")} className="ml-2 hidden md:block">
          <ul className="flex items-center gap-1">
            {onLanding ? (
              SECTIONS.map((section) => (
                <li key={section.href}>
                  <a href={section.href} className={NAV_LINK}>
                    {t(section.label)}
                  </a>
                </li>
              ))
            ) : (
              <li>
                <Link to="/tournaments" className={NAV_LINK}>
                  {t("landing.nav.tournaments")}
                </Link>
              </li>
            )}
          </ul>
        </nav>

        <div className="ml-auto flex items-center gap-1 sm:gap-2">
          {/* The same control `AppShell` offers, not a second one. Theme and
              language are properties of this browser and stay reachable in
              every session state — A64-025.9B's rule, and dropping it here
              would be the landing page quietly regressing it. */}
          <BrowserSettingsMenu />

          {offerAuth && (
            <>
              <Button asChild variant="ghost" size="sm" className="hidden sm:inline-flex">
                <Link to="/login">{t("auth.login.submit")}</Link>
              </Button>
              <Button asChild size="sm">
                <Link to="/register">{t("landing.cta.primary")}</Link>
              </Button>
            </>
          )}

          {/* The mobile disclosure. A `<button>` with `aria-expanded` and
              `aria-controls`, not a checkbox hack: it has to be reachable
              by keyboard and announced as what it is. */}
          {/* Off the landing page the panel would hold one link and, for a
              signed-in reader, nothing at all — so the control that opens
              it is not drawn. A disclosure button for an empty panel is a
              button that appears to do nothing. */}
          {(onLanding || offerAuth) && (
            <Button
              variant="ghost"
              size="icon"
              className="md:hidden"
              aria-expanded={menuOpen}
              aria-controls="landing-menu"
              aria-label={t("landing.nav.toggle")}
              onClick={() => setMenuOpen((open) => !open)}
            >
              <MenuIcon aria-hidden="true" className="size-5" />
            </Button>
          )}
        </div>
      </div>

      {/* Rendered rather than hidden with CSS, so a closed menu holds no
          focusable elements — a `display:none` panel is invisible and a
          `hidden`-class one is often not. */}
      {menuOpen && (
        <nav
          id="landing-menu"
          aria-label={t("landing.nav.label")}
          className="border-border bg-background border-t md:hidden"
        >
          <ul className="mx-auto flex max-w-6xl flex-col px-4 py-2">
            {onLanding ? (
              SECTIONS.map((section) => (
                <li key={section.href}>
                  <a
                    href={section.href}
                    onClick={() => setMenuOpen(false)}
                    className={MENU_LINK}
                  >
                    {t(section.label)}
                  </a>
                </li>
              ))
            ) : (
              <li>
                <Link
                  to="/tournaments"
                  onClick={() => setMenuOpen(false)}
                  className={MENU_LINK}
                >
                  {t("landing.nav.tournaments")}
                </Link>
              </li>
            )}
            {offerAuth && (
              <li className="sm:hidden">
                <Link to="/login" onClick={() => setMenuOpen(false)} className={MENU_LINK}>
                  {t("auth.login.submit")}
                </Link>
              </li>
            )}
          </ul>
        </nav>
      )}
    </header>
  );
}

// Literal, because Tailwind v4 scans source text: a class name assembled at
// runtime generates no CSS. Extracted only after the third identical copy.
const NAV_LINK =
  "text-muted-foreground hover:text-foreground focus-visible:ring-ring flex min-h-11 items-center rounded-md px-3 text-sm font-medium transition-colors duration-fast focus-visible:ring-2 focus-visible:outline-none";

const MENU_LINK =
  "focus-visible:ring-ring flex min-h-11 items-center rounded-md text-sm font-medium focus-visible:ring-2 focus-visible:outline-none";

/** In-page anchors, rendered on `/` only — see the docstring. */
const SECTIONS = [
  { href: "#play", label: "landing.nav.play" },
  { href: "#compete", label: "landing.nav.compete" },
  { href: "#tournaments", label: "landing.nav.tournaments" },
] as const;
