import { Link } from "@tanstack/react-router";
import type { ReactNode } from "react";

import { useTranslation } from "@/shared/i18n";

/**
 * The frame every social page renders in.
 *
 * Mirrors `SettingsShell` deliberately: a scrolling row of links below `md`
 * and a sidebar above it. Two settings-like sections that navigate
 * differently is a platform that feels assembled from parts.
 *
 * `aria-current="page"` on the active link, so a screen-reader user gets
 * what the highlight gives everybody else — and the only signal that
 * survives at high contrast.
 */
const LINKS = [
  { to: "/friends", key: "social.nav.friends" },
  { to: "/friends/requests", key: "social.nav.requests" },
  // A64-022.5 §2. Beside the request lists rather than under `/play`: a
  // challenge is directed at a person you already know, which is what every
  // other entry here is about. The lobby is where you look for a stranger.
  { to: "/challenges", key: "social.nav.challenges" },
  { to: "/friends/blocked", key: "social.nav.blocked" },
  { to: "/search", key: "social.nav.search" },
] as const;

export function SocialNav({
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
      <nav aria-label={t("social.nav.title")} className="md:w-52 md:shrink-0">
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
