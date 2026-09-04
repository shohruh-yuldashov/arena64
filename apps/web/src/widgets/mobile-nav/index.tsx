import { Link } from "@tanstack/react-router";
import { MenuIcon } from "lucide-react";
import { useState } from "react";

import { isAuthenticated } from "@/entities/session";
import { useSession } from "@/features/auth/model/session-provider";
import { type TranslationKey, useTranslation } from "@/shared/i18n";
import { cn } from "@/shared/lib/cn";
import {
  Button,
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
  Spinner,
} from "@/shared/ui";
import { useSignOutAction } from "@/widgets/account-menu/model";
import { NAV_SECTIONS, useActiveSection } from "@/widgets/primary-nav/model";

/** Account destinations, in the order §9 puts them. */
const ACCOUNT_LINKS: readonly { to: string; label: TranslationKey }[] = [
  { to: "/profile", label: "profile.nav.profile" },
  { to: "/settings/profile", label: "profile.nav.editProfile" },
];

/**
 * The product's navigation, on a phone — A64-025.3 §8.
 *
 * `specs/product-experience.md` P1-1: the header put a brand, three
 * labelled nav buttons, an avatar, a sign-out button, a bell and a theme
 * toggle in one 56px row, and the only responsive rule in it hid the user's
 * *name*. At 360px that is eight interactive elements and a wordmark
 * competing for 360 pixels, in a language whose word for "Tournaments" is
 * `Turnirlar`.
 *
 * ## A dialog, not a new dependency
 *
 * §8 asks for a panel and asks to check the existing stack first. Radix
 * Dialog is already here and already wrapped in `shared/ui`, and it brings
 * the four things a navigation panel must not get wrong: a focus trap while
 * open, focus returned to the trigger on close, `Escape`, and
 * `aria-expanded`/`aria-controls` wired between trigger and content. A
 * hand-rolled sheet would be all four of those written again, less well.
 *
 * The panel is styled as a full-height sheet rather than a centred card,
 * which is a `className` on `DialogContent` — not a second primitive.
 *
 * ## Closing on navigation
 *
 * A route change with the panel still open leaves a player looking at a
 * menu on top of the page they just asked for. `onSelect` closes it, and
 * because Radix returns focus to the trigger the keyboard lands somewhere
 * predictable rather than at the top of the document.
 */
export function MobileNav() {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const active = useActiveSection();
  const { state } = useSession();
  const { signingOut, signOut } = useSignOutAction();
  const signedIn = isAuthenticated(state);

  // A64-025.4B §29. Nothing to open when signed out: every product section
  // is `protectedPage` and the account links below are already gated, so
  // the panel would be a title and a list of redirects to the sign-in
  // screen the visitor is probably already on.
  if (!signedIn) return null;

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button
          variant="ghost"
          size="sm"
          aria-label={t("layout.openMenu")}
          className="min-h-11 md:hidden"
        >
          <MenuIcon aria-hidden="true" className="size-5" />
        </Button>
      </DialogTrigger>

      <DialogContent
        className={cn(
          // A sheet from the left: full height, its own width, square on
          // the leading edge. `max-w-[calc(100%-2rem)]` from the primitive
          // still applies, so it can never exceed the viewport.
          "top-0 left-0 h-dvh w-72 max-w-[85vw] translate-x-0 translate-y-0 rounded-none",
          "grid-rows-[auto_1fr] gap-6 p-6",
          // The panel is the only thing on screen; the page behind it must
          // not scroll under it.
          "overflow-y-auto",
        )}
      >
        <DialogHeader>
          <DialogTitle>{t("layout.title")}</DialogTitle>
          <DialogDescription>{t("layout.navDescription")}</DialogDescription>
        </DialogHeader>

        <div className="flex flex-col gap-6">
          <nav aria-label={t("layout.primaryNav")}>
            <ul className="flex flex-col gap-1">
              {NAV_SECTIONS.map((section) => {
                const current = section.to === active;
                return (
                  <li key={section.to}>
                    <Link
                      to={section.to}
                      onClick={() => setOpen(false)}
                      aria-current={current ? "page" : undefined}
                      className={cn(
                        "focus-visible:ring-ring flex min-h-11 items-center rounded-md px-3 text-sm focus-visible:ring-2 focus-visible:outline-none",
                        current
                          ? "bg-muted text-foreground font-medium"
                          : "text-muted-foreground hover:text-foreground",
                      )}
                    >
                      {t(section.label)}
                    </Link>
                  </li>
                );
              })}
            </ul>
          </nav>

          {/* Its own landmark with its own name, so the panel does not
              present the account as one more product section — the
              separation §9 asks for, kept on the surface where both had to
              share one panel. Absent when signed out: there is no account
              to reach. */}
          {signedIn && (
            <nav aria-label={t("layout.accountNav")} className="border-t pt-4">
              <ul className="flex flex-col gap-1">
                {ACCOUNT_LINKS.map((link) => (
                  <li key={link.to}>
                    <Link
                      to={link.to}
                      onClick={() => setOpen(false)}
                      className="text-muted-foreground hover:text-foreground focus-visible:ring-ring flex min-h-11 items-center rounded-md px-3 text-sm focus-visible:ring-2 focus-visible:outline-none"
                    >
                      {t(link.label)}
                    </Link>
                  </li>
                ))}
                <li>
                  <Button
                    variant="ghost"
                    className="min-h-11 w-full justify-start px-3 font-normal"
                    disabled={signingOut}
                    onClick={() => void signOut()}
                  >
                    {signingOut ? (
                      <Spinner label={t("auth.common.submitting")} />
                    ) : (
                      t("auth.session.signOut")
                    )}
                  </Button>
                </li>
              </ul>
            </nav>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}
