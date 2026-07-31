import { NextIntlClientProvider } from "next-intl";
import type { ReactNode } from "react";

/**
 * Bridges the locale and messages resolved server-side (`src/i18n/request.ts`,
 * via the next-intl plugin in `next.config.ts`) down to Client Components
 * that call `useTranslations()`. Needs no explicit `locale`/`messages`
 * props — next-intl reads them from the request context automatically.
 *
 * Deliberately kept outside `AppProviders`' `"use client"` boundary:
 * `NextIntlClientProvider` itself is what turns server-resolved messages
 * into client-readable context, so it belongs at the point where server
 * and client rendering meet, not nested inside an already-client subtree.
 */
export function LocaleProvider({ children }: { children: ReactNode }) {
  return <NextIntlClientProvider>{children}</NextIntlClientProvider>;
}
