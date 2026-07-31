import { useTranslations } from "next-intl";

/**
 * A Server Component — `useTranslations` from `next-intl` works
 * synchronously in Server Components (it reads request-scoped context set
 * up by `src/i18n/request.ts`), so this needs no `"use client"` and ships
 * zero JavaScript, matching this app's "Server Components by default"
 * requirement.
 */
export function SiteFooter() {
  const t = useTranslations("layout");
  const year = new Date().getFullYear();

  return (
    <footer className="border-border border-t">
      <div className="text-muted-foreground mx-auto max-w-6xl px-4 py-6 text-sm">
        © {year} {t("title")}
      </div>
    </footer>
  );
}
