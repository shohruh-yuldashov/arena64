import { Link } from "@tanstack/react-router";

import { useTranslation } from "@/shared/i18n";
import { cn } from "@/shared/lib/cn";
import { NAV_SECTIONS, useActiveSection } from "@/widgets/primary-nav/model";

/**
 * The product's navigation, on a wide screen — A64-025.3 §4, §6, §7.
 *
 * A `<nav>` with its own accessible name, because the shell has more than
 * one navigation and a screen reader lists them by name — the account menu
 * is the other, and "Main" versus "Account" is the difference between a
 * useful landmark list and two identical entries.
 *
 * ## `aria-current`, and why it is not `activeProps`
 *
 * `SocialNav` and `SettingsShell` set `aria-current` through TanStack's
 * `activeProps`, and that is right for them: one link, one route. It is
 * wrong here, because a section owns routes its link does not point at —
 * `Social` must be current on `/challenges`, whose link is elsewhere
 * entirely. `useActiveSection` asks the router which section matched and
 * this renders that answer.
 *
 * Both the word and the weight change, never colour alone (§12).
 *
 * Hidden below `md`, where `MobileNav` takes over. Hidden rather than
 * reflowed: a row of four labelled links plus a brand, a bell and an
 * account control does not fit 360px in any language, and Russian is the
 * one that proves it — `Турниры` beside `Друзья` beside `История`.
 */
export function PrimaryNav() {
  const { t } = useTranslation();
  const active = useActiveSection();

  return (
    <nav aria-label={t("layout.primaryNav")} className="hidden md:block">
      <ul className="flex items-center gap-1">
        {NAV_SECTIONS.map((section) => {
          const current = section.to === active;
          return (
            <li key={section.to}>
              <Link
                to={section.to}
                aria-current={current ? "page" : undefined}
                className={cn(
                  "focus-visible:ring-ring flex min-h-11 items-center rounded-md px-3 text-sm whitespace-nowrap focus-visible:ring-2 focus-visible:outline-none",
                  current
                    ? "bg-muted text-foreground font-medium"
                    : "text-muted-foreground hover:text-foreground",
                )}
              >
                {t(section.label)}
              </Link>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
