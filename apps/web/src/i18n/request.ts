import { hasLocale } from "next-intl";
import { getRequestConfig } from "next-intl/server";

import { routing } from "@/i18n/routing";

/**
 * Server-side message loading, invoked by next-intl once per request
 * (wired into next.config.ts via the next-intl plugin). Falls back to the
 * default locale rather than throwing: middleware.ts already guarantees
 * every routed request carries a valid locale segment, but this file also
 * runs for requests middleware doesn't see (e.g. `generateStaticParams`
 * during a build), so it must be correct standalone.
 */
export default getRequestConfig(async ({ requestLocale }) => {
  const requested = await requestLocale;
  const locale = hasLocale(routing.locales, requested)
    ? requested
    : routing.defaultLocale;

  const messages = (await import(`@/locales/${locale}.json`)).default;

  return { locale, messages };
});
