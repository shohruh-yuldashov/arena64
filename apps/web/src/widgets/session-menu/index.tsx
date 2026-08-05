import { Link } from "@tanstack/react-router";
import { useState } from "react";

import { displayNameOf } from "@/entities/user";
import { useSession } from "@/features/auth/model/session-provider";
import { useTranslation } from "@/shared/i18n";
import { Avatar, AvatarFallback, Button, Spinner } from "@/shared/ui";

/**
 * Who is signed in, and the way out.
 *
 * The only place `signOut` is reachable from, which is the point: an action
 * that exists and no screen exposes is the "implemented, tested, reachable
 * from nothing" failure this codebase has found twice on the backend.
 *
 * Renders **nothing** while the session is unresolved — which is both
 * `bootstrapping` and `unavailable`. A sign-in link that appeared for a
 * moment on every reload and then became a user menu is a layout shift and
 * a lie in the same flicker; and offering one because the *server* could
 * not be reached tells a signed-in player they are signed out, which is
 * the exact claim `unavailable` exists to avoid making.
 *
 * So the sign-in link appears only for `anonymous`, which is the one state
 * that actually means "there is no session".
 */
export function SessionMenu() {
  const { t } = useTranslation();
  const { state, signOut } = useSession();
  const [signingOut, setSigningOut] = useState(false);

  if (state.status === "bootstrapping" || state.status === "unavailable") return null;

  if (state.status === "anonymous") {
    return (
      <Button asChild size="sm" variant="ghost">
        <Link to="/login">{t("auth.login.submit")}</Link>
      </Button>
    );
  }

  const name = displayNameOf(state.user);

  const onSignOut = async () => {
    setSigningOut(true);
    try {
      await signOut();
    } catch {
      // `signOut` clears this device before it rethrows, so the user is
      // already signed out here — the throw only reports that the server
      // was not reached. Nothing to do but stop showing a spinner.
    } finally {
      setSigningOut(false);
    }
  };

  return (
    <div className="flex items-center gap-1 sm:gap-2">
      {/* A64-020.5A. The one entry point to the lobby — without it
          `/play` is reachable only by typing a URL, and a "start a game"
          action that no screen exposes is the failure this codebase has now
          found four times on the backend.

          First in the row, and deliberately the only `default` variant
          here: starting a game is what a player came to do, and the rest of
          this menu is navigation around it. */}
      <Button asChild size="sm" className="min-h-11">
        <Link to="/play">{t("play.nav.play")}</Link>
      </Button>

      {/* A64-020.6. The one entry point to the tournament lobby, for the
          reason below: A64-019 shipped a whole tournament backend that no
          screen reached, and a bracket nobody can navigate to is a bracket
          nobody has. */}
      <Button asChild size="sm" variant="ghost" className="min-h-11">
        <Link to="/tournaments">{t("tournament.nav")}</Link>
      </Button>

      {/* A64-020.4. The one entry point to the social pages — without it
          `/friends` and `/search` are reachable only by typing a URL, which
          is the "implemented and reachable from nothing" failure this
          codebase has now found three times on the backend. */}
      <Button asChild size="sm" variant="ghost" className="min-h-11">
        <Link to="/friends">{t("social.nav.friends")}</Link>
      </Button>

      {/* The avatar and the name are one link to the profile — the way a
          signed-in player reaches their own page from anywhere. */}
      <Button asChild size="sm" variant="ghost" className="min-h-11 gap-2 px-2">
        <Link to="/profile">
          <Avatar className="size-6">
            <AvatarFallback aria-hidden="true">{name.slice(0, 2).toUpperCase()}</AvatarFallback>
          </Avatar>
          <span className="hidden text-sm font-medium sm:inline">{name}</span>
        </Link>
      </Button>
      <Button
        size="sm"
        variant="ghost"
        className="min-h-11"
        disabled={signingOut}
        onClick={() => void onSignOut()}
      >
        {signingOut ? (
          <Spinner label={t("auth.common.submitting")} />
        ) : (
          t("auth.session.signOut")
        )}
      </Button>
    </div>
  );
}
