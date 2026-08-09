import { Link } from "@tanstack/react-router";

import { displayNameOf } from "@/entities/user";
import { useSession } from "@/features/auth/model/session-provider";
import { useTranslation } from "@/shared/i18n";
import { Avatar, AvatarFallback, Button, Spinner } from "@/shared/ui";
import { useSignOutAction } from "@/widgets/account-menu/model";

/**
 * Who is signed in, and the way out — A64-025.3 §9.
 *
 * ## What this is no longer
 *
 * It was `SessionMenu`, and it had become the product's entire navigation:
 * Play, Tournaments and Friends were buttons inside a component named after
 * the session (`specs/product-experience.md` §3.3). Nobody adding a section
 * would think to look in it, which is the most likely reason
 * `/games/history` never got one.
 *
 * Product navigation moved to `PrimaryNav` and `MobileNav`. What is left
 * here is what the name always promised: the account. Profile — which is
 * where Settings is reached from — and sign-out.
 *
 * ## The states are unchanged
 *
 * Renders **nothing** while the session is unresolved, which is both
 * `bootstrapping` and `unavailable`. A sign-in link that appeared for a
 * moment on every reload and then became a user menu is a layout shift and
 * a lie in the same flicker; and offering one because the *server* could not
 * be reached tells a signed-in player they are signed out, which is the
 * exact claim `unavailable` exists to avoid making.
 *
 * So the sign-in link appears only for `anonymous`, the one state that
 * actually means "there is no session". None of that is A64-025.3's to
 * change.
 *
 * ## Sign-out is here and in the mobile panel
 *
 * Two surfaces, never both mounted, sharing `useSignOutAction` — see there
 * for why the shared piece is the action rather than the component.
 */
export function AccountMenu() {
  const { t } = useTranslation();
  const { state } = useSession();
  const { signingOut, signOut } = useSignOutAction();

  if (state.status === "bootstrapping" || state.status === "unavailable") return null;

  if (state.status === "anonymous") {
    return (
      <Button asChild size="sm" variant="ghost" className="min-h-11">
        <Link to="/login">{t("auth.login.submit")}</Link>
      </Button>
    );
  }

  const name = displayNameOf(state.user);

  return (
    <>
      {/* The avatar and the name are one link to the profile — the way a
          signed-in player reaches their own page from anywhere. The name
          is hidden on a phone; the avatar is the affordance there. */}
      <Button asChild size="sm" variant="ghost" className="min-h-11 gap-2 px-2">
        <Link to="/profile">
          <Avatar className="size-6">
            <AvatarFallback aria-hidden="true">{name.slice(0, 2).toUpperCase()}</AvatarFallback>
          </Avatar>
          {/* A64-025.6: `lg`, not `sm`. At 768 the header carries the
              wordmark, four nav sections, the bell, the avatar, the name and
              a sign-out button — measured at 110px of horizontal overflow on
              a signed-in game room, which A64-025.3 missed because it
              measured 768 signed *out*. The avatar is the identity there;
              the name returns when there is room for it. */}
          <span className="hidden text-sm font-medium lg:inline">{name}</span>
        </Link>
      </Button>

      {/* Below `md` this lives in the mobile panel instead, where there is
          room for a labelled control rather than a squeezed one. */}
      <Button
        size="sm"
        variant="ghost"
        className="hidden min-h-11 md:inline-flex"
        disabled={signingOut}
        onClick={() => void signOut()}
      >
        {signingOut ? (
          <Spinner label={t("auth.common.submitting")} />
        ) : (
          t("auth.session.signOut")
        )}
      </Button>
    </>
  );
}
