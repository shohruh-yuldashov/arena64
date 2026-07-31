"use client";

import { useTranslations } from "next-intl";
import { useEffect } from "react";

import { Button } from "@/components/ui/button";
import { Link } from "@/i18n/navigation";

interface RouteErrorProps {
  error: Error & { digest?: string };
  reset: () => void;
}

/**
 * The error boundary for everything under this locale segment. Must be a
 * Client Component — Next.js requires it, since `error.tsx` is a React
 * error boundary and boundaries only exist on the client. Nested one
 * level below the root layout's providers, so `useTranslations` still
 * resolves correctly even though the tree above this point just threw.
 */
export default function LocaleError({ error, reset }: RouteErrorProps) {
  const t = useTranslations("error");
  const tc = useTranslations("common");

  useEffect(() => {
    // Structured, not console.log — matches CLAUDE.md §8's logging rules
    // even though there is no backend log pipeline for the client yet.
    // A real reporting sink (Sentry or equivalent) is a follow-up; this
    // is the one call site that would change when it exists.
    console.error("route_error", { message: error.message, digest: error.digest });
  }, [error]);

  return (
    <div className="mx-auto flex max-w-6xl flex-col items-start gap-4 px-4 py-24">
      <h1 className="text-3xl font-semibold tracking-tight">{t("title")}</h1>
      <p className="text-muted-foreground max-w-prose">{t("description")}</p>
      <div className="flex gap-3">
        <Button onClick={reset}>{tc("retry")}</Button>
        <Button variant="outline" asChild>
          <Link href="/">{tc("goHome")}</Link>
        </Button>
      </div>
    </div>
  );
}
