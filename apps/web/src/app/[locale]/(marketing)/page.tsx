import { hasLocale } from "next-intl";
import { getTranslations, setRequestLocale } from "next-intl/server";
import { notFound } from "next/navigation";

import { routing } from "@/i18n/routing";

interface HomePageProps {
  params: Promise<{ locale: string }>;
}

/**
 * The foundation's one real page — a Server Component rendering the two
 * messages already defined in every locale (`layout.title`,
 * `layout.description`), which is enough to prove the full pipeline works
 * end to end (SSR, i18n, theme, Tailwind) without building a feature.
 * Deliberately not a "home page design" — that is product work this task
 * excludes.
 */
export default async function HomePage({ params }: HomePageProps) {
  const { locale } = await params;
  if (!hasLocale(routing.locales, locale)) {
    notFound();
  }

  setRequestLocale(locale);

  const t = await getTranslations("layout");

  return (
    <div className="mx-auto flex max-w-6xl flex-col items-start gap-4 px-4 py-24">
      <h1 className="text-4xl font-semibold tracking-tight text-balance">{t("title")}</h1>
      <p className="text-muted-foreground max-w-prose text-lg text-balance">
        {t("description")}
      </p>
    </div>
  );
}
