import type en from "@/locales/en.json";
import type { AppLocale } from "@/i18n/routing";

/**
 * Type-safe translation keys — CLAUDE.md §2.4, "make illegal states
 * unrepresentable." Without this, `useTranslations("typo")` or
 * `t("common.mispelled")` fails only at runtime, in whichever locale a
 * reviewer happens to be testing in. `en.json` is the shape contract; `ru`
 * and `uz` are structurally checked against it by next-intl at build time.
 */
declare module "next-intl" {
  interface AppConfig {
    Locale: AppLocale;
    Messages: typeof en;
  }
}
