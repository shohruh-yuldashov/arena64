import { defineRouting } from "next-intl/routing";

/**
 * The single source of truth for which locales exist and how URLs express
 * them (CLAUDE.md §2.1 — "one source of truth per concept"). Every other
 * i18n file (`navigation.ts`, `request.ts`, `middleware.ts`) derives from
 * this rather than repeating the locale list.
 */
export const routing = defineRouting({
  locales: ["en", "ru", "uz"],
  defaultLocale: "en",

  // `always`: every URL carries its locale (`/en`, `/ru`, `/uz`), including
  // the default. The alternative — hiding the default locale's prefix — is
  // one fewer character in the common case, at the cost of an ambiguity a
  // clocked, competitive platform can't afford: a bare `/match/abc123`
  // becomes indistinguishable between "no locale resolved yet" and "the
  // default locale, silently." Explicit prefixes make the locale a fact of
  // the URL a server component can read synchronously, not a fact that
  // depends on which locale happens to be default this month.
  localePrefix: "always",
});

export type AppLocale = (typeof routing.locales)[number];
