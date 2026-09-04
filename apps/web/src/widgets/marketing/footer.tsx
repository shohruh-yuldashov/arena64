import { Link } from "@tanstack/react-router";

import { useTranslation } from "@/shared/i18n";

/**
 * The public footer — A64-026.1 §40.9, moved out of the landing page in
 * A64-026.4 §43.5 when a second page started needing it.
 *
 * **Only real destinations.** There is no privacy page, no terms page, no
 * blog, no Discord and no official social account, so none of them is
 * linked. A footer column of dead links is worse than a short footer.
 *
 * The year is computed rather than written, because a hardcoded one is
 * wrong from January.
 *
 * ## `onLanding`, for the header's reason
 *
 * Two of these were in-page anchors, and an in-page anchor is only a
 * destination on the page that holds the section. Off the landing page they
 * are replaced by the one link that is a route everywhere — the same rule
 * `MarketingHeader` follows, stated once there.
 */
export function MarketingFooter({ onLanding }: { onLanding: boolean }) {
  const { t } = useTranslation();

  return (
    <footer className="border-border border-t">
      <div className="text-muted-foreground mx-auto flex max-w-6xl flex-col gap-4 px-4 py-8 text-sm sm:flex-row sm:items-center sm:justify-between">
        <div className="flex flex-col gap-1">
          <span className="text-foreground font-semibold">{t("layout.title")}</span>
          <span className="text-xs">
            © {new Date().getFullYear()} {t("layout.title")}
          </span>
        </div>

        {/* `min-h-11` on each link: A64-025.13 §35.3 measured the product's
            floor at 44px and found sixteen controls under it. A footer row
            of 17px text links is the same defect in a quieter place. */}
        <nav aria-label={t("landing.footer.label")}>
          <ul className="flex flex-wrap items-center gap-x-5 gap-y-2">
            {onLanding && (
              <li>
                <a href="#play" className={FOOTER_LINK}>
                  {t("landing.nav.play")}
                </a>
              </li>
            )}
            <li>
              {onLanding ? (
                <a href="#tournaments" className={FOOTER_LINK}>
                  {t("landing.nav.tournaments")}
                </a>
              ) : (
                <Link to="/tournaments" className={FOOTER_LINK}>
                  {t("landing.nav.tournaments")}
                </Link>
              )}
            </li>
            <li>
              <Link to="/login" className={FOOTER_LINK}>
                {t("auth.login.submit")}
              </Link>
            </li>
            <li>
              <Link to="/register" className={FOOTER_LINK}>
                {t("landing.cta.primary")}
              </Link>
            </li>
          </ul>
        </nav>
      </div>
    </footer>
  );
}

// Literal, because Tailwind v4 scans source text and generates nothing for a
// class name assembled at runtime.
const FOOTER_LINK =
  "hover:text-foreground focus-visible:ring-ring inline-flex min-h-11 items-center rounded-md transition-colors duration-fast focus-visible:ring-2 focus-visible:outline-none";
