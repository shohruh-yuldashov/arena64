import { hasLocale } from "next-intl";
import { getTranslations, setRequestLocale } from "next-intl/server";
import { Inter } from "next/font/google";
import { notFound } from "next/navigation";
import type { Metadata } from "next";
import type { ReactNode } from "react";

import { AppProviders } from "@/providers/app-providers";
import { LocaleProvider } from "@/providers/locale-provider";
import { routing } from "@/i18n/routing";

import "@/styles/globals.css";

const inter = Inter({
  subsets: ["latin", "cyrillic"],
  variable: "--font-sans",
  display: "swap",
});

// Pre-renders `/en`, `/ru`, `/uz` at build time rather than resolving the
// locale on first request — the standard next-intl + App Router pairing
// with static rendering.
export function generateStaticParams() {
  return routing.locales.map((locale) => ({ locale }));
}

interface LocaleLayoutProps {
  children: ReactNode;
  params: Promise<{ locale: string }>;
}

export async function generateMetadata({
  params,
}: Pick<LocaleLayoutProps, "params">): Promise<Metadata> {
  const { locale } = await params;
  if (!hasLocale(routing.locales, locale)) {
    notFound();
  }

  const t = await getTranslations({ locale, namespace: "layout" });

  return {
    title: {
      default: t("title"),
      template: `%s · ${t("title")}`,
    },
    description: t("description"),
  };
}

export default async function LocaleLayout({ children, params }: LocaleLayoutProps) {
  const { locale } = await params;

  if (!hasLocale(routing.locales, locale)) {
    notFound();
  }

  // Enables static rendering: without this, reading the locale anywhere
  // in the tree opts the whole route into dynamic rendering
  // (next-intl's documented requirement for this pattern).
  setRequestLocale(locale);

  return (
    <html lang={locale} className={inter.variable} suppressHydrationWarning>
      {/*
       * suppressHydrationWarning is scoped to this element only — it does
       * not silence mismatches anywhere else. It exists because
       * next-themes sets `class` on <html> from localStorage before React
       * hydrates, which is an intentional, expected mismatch (see
       * providers/theme-provider.tsx).
       */}
      <body className="bg-background text-foreground min-h-screen font-sans antialiased">
        <LocaleProvider>
          <AppProviders>{children}</AppProviders>
        </LocaleProvider>
      </body>
    </html>
  );
}
