import { Link } from "@tanstack/react-router";
import type { ReactNode } from "react";

import { useTranslation } from "@/shared/i18n";

/**
 * The frame every authentication page renders in — A64-025.4 §3.
 *
 * A widget rather than a layout route: these pages are reached from
 * anywhere, including from a mail client, and a route-level layout would
 * make the shell part of the route tree — which is where a redirect loop
 * gets built when a guard and a layout disagree about who owns `/login`.
 *
 * The `<h1>` lives here, once, so every auth page has exactly one and it is
 * the page's own title. A card with a heading inside a page with another
 * heading is the most common way a form ends up with two.
 *
 * ## The composition — what changed and why
 *
 * It was a `max-w-sm` card floating in the middle of a very wide page: the
 * generic developer form, identical to every other framework's starter.
 * Arena64 is a competitive board game and its front door said nothing about
 * either half of that.
 *
 * Now it is one composed surface. Above `lg` it splits: an identity panel
 * carrying the wordmark and the product's own sentence, and the form beside
 * it. Below `lg` the panel is gone and the form is the page, with the
 * wordmark above it — the same DOM, one breakpoint, no second layout.
 *
 * ## The board is the decoration, and it is the only one
 *
 * The panel sits on `--primary`, and the grid over it is one
 * `repeating-conic-gradient` of white at seven per cent — so it follows the
 * brand into dark mode without a second value to keep in step. No image, no
 * illustration, no gradient
 * blob, no dependency — the product's actual subject, quiet enough to read
 * a form beside. It is `aria-hidden`: it says nothing a screen reader needs,
 * and the panel's text says it in words anyway.
 *
 * §17 rules out a hero, a carousel, particles and a counter. This is the
 * line between identity and decoration: an eight-by-eight grid is what the
 * product *is*.
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
    <div className="mx-auto flex w-full max-w-4xl flex-col gap-6 py-4 sm:py-8">
      {/* The wordmark, on the surface where the header's own is a long way
          from the form. Hidden above `lg`, where the panel carries it. */}
      <Link
        to="/"
        className="focus-visible:ring-ring self-center rounded-md focus-visible:ring-2 focus-visible:outline-none lg:hidden"
      >
        {/* The same treatment `Brand` gives it in the header — the two are
            the one wordmark, and this page shows both at once. */}
        <span className="brand-gradient-text text-sm font-semibold tracking-tight">
          {t("layout.title")}
        </span>
      </Link>

      <div className="border-border bg-card grid overflow-hidden rounded-xl border shadow-sm lg:grid-cols-[1fr_1.1fr]">
        {/* --- identity, wide screens only ---------------------------- */}
        {/* The brand gradient rather than the flat brand colour — A64-025.9
            §18.7. It replaces `bg-primary` on the same element with the same
            foreground, so the contrast this panel already had is the
            contrast it keeps: both ends of the ramp clear 4.5:1 against
            `--primary-foreground` on their own. */}
        <aside className="brand-gradient text-primary-foreground relative hidden flex-col justify-between p-8 lg:flex">
          <div
            aria-hidden="true"
            className="pointer-events-none absolute inset-0 opacity-[0.07]"
            style={{
              backgroundImage: "repeating-conic-gradient(#fff 0% 25%, transparent 0% 50%)",
              backgroundSize: "4rem 4rem",
            }}
          />

          <span className="relative text-lg font-semibold tracking-tight">
            {t("layout.title")}
          </span>

          <p className="relative max-w-xs text-sm leading-relaxed opacity-90">
            {t("layout.description")}
          </p>
        </aside>

        {/* --- the form ----------------------------------------------- */}
        <div className="flex flex-col justify-center p-6 sm:p-8">
          <div className="mx-auto flex w-full max-w-sm flex-col gap-6">
            <div className="flex flex-col gap-1.5">
              <h1 className="text-2xl font-semibold tracking-tight">{title}</h1>
              {description !== undefined && (
                <p className="text-muted-foreground text-sm">{description}</p>
              )}
            </div>

            {children}
          </div>
        </div>
      </div>

      {footer !== undefined && (
        <div className="text-muted-foreground text-center text-sm">{footer}</div>
      )}
    </div>
  );
}
