import { Link } from "@tanstack/react-router";
import type { ReactNode } from "react";

import { useTranslation } from "@/shared/i18n";

/**
 * The frame for every `/settings/*` page.
 *
 * ## Layout, at three sizes
 *
 * Below `sm` the navigation is a horizontal scrolling row above the
 * content — a vertical sidebar at 360px would eat a third of the width for
 * five links. From `md` it becomes a sidebar beside the content. Nothing
 * between needs a third arrangement.
 *
 * ## `aria-current`, not just a colour
 *
 * `activeProps` sets `aria-current="page"` on the link for the page being
 * viewed. A screen-reader user gets the same information the highlight
 * gives everyone else — and it is the only signal that survives at high
 * contrast.
 */
const LINKS = [
  { to: "/settings/profile", key: "profile.nav.editProfile" },
  { to: "/settings/preferences", key: "profile.nav.preferences" },
  { to: "/settings/privacy", key: "profile.nav.privacy" },
  { to: "/settings/notifications", key: "profile.nav.notifications" },
  { to: "/settings/sessions", key: "profile.nav.sessions" },
] as const;

export function SettingsShell({
  title,
  description,
  children,
}: {
  title: string;
  description?: string;
  children: ReactNode;
}) {
  const { t } = useTranslation();

  return (
    <div className="flex flex-col gap-6 md:flex-row md:gap-10">
      <nav aria-label={t("profile.nav.settings")} className="md:w-52 md:shrink-0">
        <ul className="-mx-1 flex gap-1 overflow-x-auto px-1 pb-1 md:flex-col md:overflow-visible md:pb-0">
          {LINKS.map((link) => (
            <li key={link.to} className="shrink-0">
              <Link
                to={link.to}
                className="text-muted-foreground hover:text-foreground focus-visible:ring-ring flex min-h-11 items-center rounded-md px-3 text-sm whitespace-nowrap focus-visible:ring-2 focus-visible:outline-none"
                activeProps={{
                  className:
                    "bg-muted text-foreground flex min-h-11 items-center rounded-md px-3 text-sm font-medium whitespace-nowrap",
                  "aria-current": "page",
                }}
              >
                {t(link.key)}
              </Link>
            </li>
          ))}
        </ul>
      </nav>

      <section className="min-w-0 flex-1">
        <h1 className="text-xl font-semibold">{title}</h1>
        {description !== undefined && (
          <p className="text-muted-foreground mt-1 text-sm">{description}</p>
        )}
        <div className="mt-6">{children}</div>
      </section>
    </div>
  );
}
