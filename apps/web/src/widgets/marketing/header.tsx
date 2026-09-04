import { Link } from "@tanstack/react-router";
import { MenuIcon } from "lucide-react";
import { useEffect, useState } from "react";

import { useSession } from "@/features/auth/model/session-provider";
import { useTranslation } from "@/shared/i18n";
import { cn } from "@/shared/lib/cn";
import { Button } from "@/shared/ui";
import { BrowserSettingsMenu } from "@/widgets/account-menu";

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
 * **Every product route is behind a guard.** `/tournaments`, `/play`,
 * `/friends` all redirect an anonymous visitor to sign-in — so a "Browse
 * tournaments" link in this header would be a link that lies about where it
 * goes, which is the defect A64-025.3 §2 refused to ship on the home page.
 *
 * So the links are in-page anchors to the sections that explain those
 * features. A visitor gets taken to the explanation; the CTA takes them to
 * the account that unlocks the thing itself.
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
  const [scrolled, setScrolled] = useState(false);

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
        <Link
          to="/"
          className="focus-visible:ring-ring flex min-h-11 items-center rounded-md pr-2 focus-visible:ring-2 focus-visible:outline-none"
          aria-label={t("layout.home")}
        >
          <span className="brand-gradient-text text-base font-semibold tracking-tight">
            {t("layout.title")}
          </span>
        </Link>

        <nav aria-label={t("landing.nav.label")} className="ml-2 hidden md:block">
          <ul className="flex items-center gap-1">
            {SECTIONS.map((section) => (
              <li key={section.href}>
                <a
                  href={section.href}
                  className="text-muted-foreground hover:text-foreground focus-visible:ring-ring flex min-h-11 items-center rounded-md px-3 text-sm font-medium transition-colors duration-fast focus-visible:ring-2 focus-visible:outline-none"
                >
                  {t(section.label)}
                </a>
              </li>
            ))}
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
            {SECTIONS.map((section) => (
              <li key={section.href}>
                <a
                  href={section.href}
                  onClick={() => setMenuOpen(false)}
                  className="focus-visible:ring-ring flex min-h-11 items-center rounded-md text-sm font-medium focus-visible:ring-2 focus-visible:outline-none"
                >
                  {t(section.label)}
                </a>
              </li>
            ))}
            {offerAuth && (
              <li className="sm:hidden">
                <Link
                  to="/login"
                  onClick={() => setMenuOpen(false)}
                  className="focus-visible:ring-ring flex min-h-11 items-center rounded-md text-sm font-medium focus-visible:ring-2 focus-visible:outline-none"
                >
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

/** In-page anchors. Every product route is guarded — see the docstring. */
const SECTIONS = [
  { href: "#play", label: "landing.nav.play" },
  { href: "#compete", label: "landing.nav.compete" },
  { href: "#tournaments", label: "landing.nav.tournaments" },
] as const;
