"use client";

import { Menu, X } from "lucide-react";
import { useTranslations } from "next-intl";

import { LocaleSwitcher } from "@/components/locale-switcher";
import { ThemeToggle } from "@/components/theme-toggle";
import { Button } from "@/components/ui/button";
import { Link } from "@/i18n/navigation";
import { useUiStore } from "@/stores/ui-store";

/**
 * The one persistent piece of chrome every route shares. No navigation
 * links to `/play`, `/profile`, and so on — those routes don't exist yet
 * (this task's "Do NOT implement" list) — so the header is brand, locale,
 * and theme only until a real feature earns a link.
 */
export function SiteHeader() {
  const t = useTranslations("layout");
  const isMobileNavOpen = useUiStore((state) => state.isMobileNavOpen);
  const toggleMobileNav = useUiStore((state) => state.toggleMobileNav);

  return (
    <header className="border-border bg-background/95 supports-[backdrop-filter]:bg-background/60 sticky top-0 z-40 border-b backdrop-blur">
      <div className="mx-auto flex h-14 max-w-6xl items-center justify-between gap-4 px-4">
        <Link href="/" className="text-base font-semibold tracking-tight">
          {t("title")}
        </Link>

        <div className="hidden items-center gap-2 sm:flex">
          <LocaleSwitcher />
          <ThemeToggle />
        </div>

        <Button
          variant="ghost"
          size="icon"
          className="sm:hidden"
          aria-label={isMobileNavOpen ? t("closeMenu") : t("openMenu")}
          aria-expanded={isMobileNavOpen}
          onClick={toggleMobileNav}
        >
          {isMobileNavOpen ? <X className="size-4" /> : <Menu className="size-4" />}
        </Button>
      </div>

      {isMobileNavOpen && (
        <div className="border-border flex items-center gap-2 border-t px-4 py-3 sm:hidden">
          <LocaleSwitcher />
          <ThemeToggle />
        </div>
      )}
    </header>
  );
}
