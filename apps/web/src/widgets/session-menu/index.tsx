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
    <div className="flex items-center gap-2">
      <Avatar>
        <AvatarFallback aria-hidden="true">{name.slice(0, 2).toUpperCase()}</AvatarFallback>
      </Avatar>
      <span className="hidden text-sm font-medium sm:inline">{name}</span>
      <Button size="sm" variant="ghost" disabled={signingOut} onClick={() => void onSignOut()}>
        {signingOut ? (
          <Spinner label={t("auth.common.submitting")} />
        ) : (
          t("auth.session.signOut")
        )}
      </Button>
    </div>
  );
}
