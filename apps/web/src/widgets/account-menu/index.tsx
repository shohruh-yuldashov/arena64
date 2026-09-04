import { Link } from "@tanstack/react-router";
import { ChevronDownIcon, LaptopIcon, MoonIcon, SunIcon } from "lucide-react";
import { useState } from "react";

import { avatarSrc, initialsOf } from "@/entities/profile";
import { displayNameOf, type User } from "@/entities/user";
import { useSession } from "@/features/auth/model/session-provider";
import { useMyProfile } from "@/features/profile/model/queries";
import { type Locale, LOCALES, type TranslationKey, useTranslation } from "@/shared/i18n";
import { cn } from "@/shared/lib/cn";
import { type ThemeMode, THEMES, useTheme } from "@/shared/theme/theme-context";
import {
  Avatar,
  AvatarFallback,
  AvatarImage,
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

/**
 * Who is signed in, and everything that belongs to *this browser* —
 * A64-025.9B §19.
 *
 * ## One control, not five
 *
 * The header carried the avatar, the name, a sign-out button and a
 * three-button theme group: five targets for one concept, and the theme
 * group alone was three of them. Sign-out sat beside the player's own name
 * as though leaving were a peer of arriving, and the language — which the
 * product ships in three of — could be changed only from
 * `/settings/preferences`, four clicks deep.
 *
 * All of it is one menu now. Theme and language stay *here* rather than in
 * settings because neither is an account preference: they are properties of
 * the browser in front of the player, stored in `localStorage`, and a
 * player who wants the interface in Russian should not have to be signed in
 * to say so. `/settings/preferences` keeps its own copy of the language
 * control, which is the account-level one.
 *
 * ## A dialog, not a new dependency
 *
 * The same decision `MobileNav` records: Radix Dialog is already here and
 * already wrapped, and it brings the four things a menu must not get wrong —
 * focus trap, focus returned to the trigger, `Escape`, and
 * `aria-expanded`/`aria-controls` wired between trigger and content. It is
 * positioned as a panel under the trigger rather than centred, which is a
 * `className` and not a second primitive.
 *
 * ## The photo, at last
 *
 * The header drew initials for every player, including the ones who had
 * uploaded a picture, because `SessionUser` carries no `avatar_url` — the
 * bootstrap response has never included one. So the panel reads
 * `/profile/me`, the query `/profile` already fills, and TanStack Query
 * serves the cached copy on every page after the first.
 *
 * It is mounted **only when authenticated**, which is what keeps an
 * anonymous visitor from firing a request that can only 401.
 *
 * ## The states are unchanged
 *
 * Renders **nothing** while the session is unresolved — both
 * `bootstrapping` and `unavailable`. A sign-in link that appeared for a
 * moment on every reload and then became a user menu is a layout shift and
 * a lie in the same flicker; and offering one because the *server* could not
 * be reached tells a signed-in player they are signed out, which is the
 * exact claim `unavailable` exists to avoid making.
 */
export function AccountMenu() {
  const { t } = useTranslation();
  const { state } = useSession();

  // Appearance and language are properties of *this browser*, so they are
  // reachable in every session state — including `unavailable`, where the
  // server could not be reached and a player staring at a broken page is
  // exactly the one who might want the interface in another language.
  if (state.status !== "authenticated") {
    return (
      <>
        <BrowserSettingsMenu />
        {/* Only for `anonymous`, the one state that actually means "there is
            no session". Offering it while bootstrapping is a flicker, and
            offering it for `unavailable` tells a signed-in player they are
            signed out — the exact claim that state exists to avoid. */}
        {state.status === "anonymous" && (
          <Button asChild size="sm" variant="ghost" className="min-h-11">
            <Link to="/login">{t("auth.login.submit")}</Link>
          </Button>
        )}
      </>
    );
  }

  return <SignedInMenu user={state.user} />;
}

function SignedInMenu({ user }: { user: User }) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const { signingOut, signOut } = useSignOutAction();
  const profile = useMyProfile();

  const name = displayNameOf(user);
  // `MyProfileResponse` carries no `avatar_version`; the cache-busting
  // suffix is `/profile`'s, where an upload has just happened. Here the
  // URL is read, never written, so a stale header photo would only ever
  // last until the query that owns it refetches.
  const src = avatarSrc(profile.data?.avatar_url);
  const initials = initialsOf({ display_name: user.display_name, username: user.username });

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button
          variant="ghost"
          size="sm"
          className="min-h-11 gap-2 px-2"
          aria-label={t("layout.accountNav")}
        >
          <Avatar className="size-7">
            {src !== null && <AvatarImage src={src} alt="" />}
            <AvatarFallback aria-hidden="true" className="text-xs">
              {initials}
            </AvatarFallback>
          </Avatar>
          {/* A64-025.6: `lg`, not `sm`. At 768 the header carries the
              wordmark, four nav sections, the bell and this — and the name
              is the part that can go without losing the affordance. */}
          <span className="hidden max-w-32 truncate text-sm font-medium lg:inline">{name}</span>
          <ChevronDownIcon aria-hidden="true" className="size-4 opacity-60" />
        </Button>
      </DialogTrigger>

      <MenuPanel title={name}>
        <div className="flex items-center gap-3 pr-6 pb-1">
          <Avatar className="size-10">
            {src !== null && <AvatarImage src={src} alt="" />}
            <AvatarFallback aria-hidden="true">{initials}</AvatarFallback>
          </Avatar>
          <div className="flex min-w-0 flex-col">
            <span className="truncate text-sm font-medium">{name}</span>
            <span className="text-muted-foreground truncate text-xs">@{user.username}</span>
          </div>
        </div>

        <MenuSection>
          <MenuLink to="/profile" onNavigate={() => setOpen(false)}>
            {t("profile.nav.profile")}
          </MenuLink>
          <MenuLink to="/settings/profile" onNavigate={() => setOpen(false)}>
            {t("profile.nav.settings")}
          </MenuLink>
        </MenuSection>

        <MenuSection>
          <ThemeChoice />
          <LocaleChoice />
        </MenuSection>

        <div className="border-border border-t pt-3">
          <Button
            variant="ghost"
            className="text-destructive hover:bg-destructive/10 hover:text-destructive min-h-11 w-full justify-start"
            disabled={signingOut}
            onClick={() => void signOut()}
          >
            {signingOut ? (
              <Spinner label={t("auth.common.submitting")} />
            ) : (
              t("auth.session.signOut")
            )}
          </Button>
        </div>
      </MenuPanel>
    </Dialog>
  );
}

/**
 * Theme and language for a visitor with no account.
 *
 * The same two controls, in the same place, signed in or out. A product
 * that only offers its own language to people who have already registered
 * has the order backwards.
 */
function BrowserSettingsMenu() {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button
          variant="ghost"
          size="icon"
          aria-label={t("layout.appearance")}
          className="min-h-11"
        >
          <SunIcon aria-hidden="true" className="size-4 dark:hidden" />
          <MoonIcon aria-hidden="true" className="hidden size-4 dark:block" />
        </Button>
      </DialogTrigger>
      <MenuPanel title={t("layout.appearance")}>
        {/* No `MenuSection` here, and the padding is deliberate. A section
            draws a rule above itself to separate it from what came before;
            in this panel nothing came before, so that rule landed at the
            very top of the popover with the dialog's own close button
            sitting on it. The top padding is what clears that button, which
            is absolutely positioned and has no header to sit beside. */}
        <div className="flex flex-col gap-1 pt-6">
          <ThemeChoice />
          <LocaleChoice />
        </div>
      </MenuPanel>
    </Dialog>
  );
}

/** The panel itself — pinned under the trigger rather than centred. */
function MenuPanel({ title, children }: { title: string; children: React.ReactNode }) {
  const { t } = useTranslation();

  return (
    <DialogContent
      className={cn(
        "top-16 right-2 bottom-auto left-auto w-72 max-w-[calc(100vw-1rem)]",
        "translate-x-0 translate-y-0 gap-3 rounded-xl p-4",
      )}
    >
      {/* Radix requires both for the dialog to be announced; neither is
          drawn here, because the panel's own contents already say what it
          is and a repeated heading is one more thing to read past. */}
      <DialogHeader className="sr-only">
        <DialogTitle>{title}</DialogTitle>
        <DialogDescription>{t("layout.accountDescription")}</DialogDescription>
      </DialogHeader>
      {children}
    </DialogContent>
  );
}

function MenuSection({ children }: { children: React.ReactNode }) {
  return <div className="border-border flex flex-col gap-1 border-t pt-3">{children}</div>;
}

function MenuLink({
  to,
  onNavigate,
  children,
}: {
  to: string;
  onNavigate: () => void;
  children: React.ReactNode;
}) {
  return (
    <Link
      to={to}
      onClick={onNavigate}
      className="focus-visible:ring-ring hover:bg-accent flex min-h-11 items-center rounded-md px-3 text-sm font-medium focus-visible:ring-2 focus-visible:outline-none"
    >
      {children}
    </Link>
  );
}

const THEME_ICONS: Record<ThemeMode, typeof SunIcon> = {
  light: SunIcon,
  dark: MoonIcon,
  system: LaptopIcon,
};

const THEME_LABELS: Record<ThemeMode, TranslationKey> = {
  light: "theme.light",
  dark: "theme.dark",
  system: "theme.system",
};

/**
 * Three explicit choices rather than one cycling button.
 *
 * A single button that rotates light → dark → system cannot say what it
 * will do next, and `system` is invisible in it: a player who wants "follow
 * my OS" has to click until they land on it and then guess whether they
 * did. So all three are still stated and the current one is marked — they
 * simply no longer cost three slots in the header.
 *
 * `aria-pressed` rather than `role="radiogroup"`: these are toggle buttons,
 * already keyboard-reachable in DOM order.
 */
function ThemeChoice() {
  const { t } = useTranslation();
  const { mode, setMode } = useTheme();

  return (
    <div className="flex items-center justify-between gap-2 px-3 py-1.5">
      <span className="text-muted-foreground text-xs font-medium">{t("theme.label")}</span>
      <div
        className="bg-muted flex items-center gap-0.5 rounded-md p-0.5"
        role="group"
        aria-label={t("theme.toggle")}
      >
        {THEMES.map((candidate) => {
          const Icon = THEME_ICONS[candidate];
          const current = mode === candidate;
          return (
            <button
              key={candidate}
              type="button"
              aria-pressed={current}
              aria-label={t(THEME_LABELS[candidate])}
              onClick={() => setMode(candidate)}
              className={cn(
                "focus-visible:ring-ring inline-flex size-8 items-center justify-center rounded focus-visible:ring-2 focus-visible:outline-none",
                current
                  ? "bg-background text-foreground shadow-sm"
                  : "text-muted-foreground hover:text-foreground",
              )}
            >
              <Icon aria-hidden="true" className="size-4" />
            </button>
          );
        })}
      </div>
    </div>
  );
}

/** The three languages the product actually ships, named in themselves. */
function LocaleChoice() {
  const { t, locale, setLocale, localeName } = useTranslation();

  return (
    <div className="flex flex-col gap-1.5 px-3 py-1.5">
      <span className="text-muted-foreground text-xs font-medium">{t("locale.label")}</span>
      <div className="bg-muted flex items-center gap-0.5 rounded-md p-0.5" role="group">
        {LOCALES.map((candidate: Locale) => {
          const current = locale === candidate;
          return (
            <button
              key={candidate}
              type="button"
              aria-pressed={current}
              onClick={() => setLocale(candidate)}
              className={cn(
                "focus-visible:ring-ring min-h-8 flex-1 rounded px-2 text-xs font-medium focus-visible:ring-2 focus-visible:outline-none",
                current
                  ? "bg-background text-foreground shadow-sm"
                  : "text-muted-foreground hover:text-foreground",
              )}
            >
              {localeName(candidate)}
            </button>
          );
        })}
      </div>
    </div>
  );
}
