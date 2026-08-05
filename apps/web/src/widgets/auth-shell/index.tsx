import { Link } from "@tanstack/react-router";
import type { ReactNode } from "react";

import { useTranslation } from "@/shared/i18n";
import { Card, CardContent, CardDescription, CardHeader } from "@/shared/ui";

/**
 * The frame every authentication page renders in.
 *
 * A widget rather than a layout route: these pages are reached from
 * anywhere, including from a mail client, and a route-level layout would
 * make the shell part of the route tree — which is where a redirect loop
 * gets built when a guard and a layout disagree about who owns `/login`.
 *
 * The `<h1>` lives here, once, so every auth page has exactly one and it is
 * the page's own title. A card with a heading inside a page with another
 * heading is the most common way a form ends up with two.
 */
export function AuthShell({
  title,
  description,
  children,
  footer,
}: {
  title: string;
  description?: string;
  children: ReactNode;
  footer?: ReactNode;
}) {
  const { t } = useTranslation();

  return (
    <div className="mx-auto flex w-full max-w-sm flex-col gap-6 py-10">
      <Link
        to="/"
        className="focus-visible:ring-ring self-center rounded-md text-sm font-semibold tracking-tight focus-visible:ring-2 focus-visible:outline-none"
      >
        {t("layout.title")}
      </Link>

      <Card>
        <CardHeader>
          <h1 className="text-xl leading-none font-semibold">{title}</h1>
          {description !== undefined && <CardDescription>{description}</CardDescription>}
        </CardHeader>
        <CardContent className="flex flex-col gap-4">{children}</CardContent>
      </Card>

      {footer !== undefined && (
        <div className="text-muted-foreground text-center text-sm">{footer}</div>
      )}
    </div>
  );
}
