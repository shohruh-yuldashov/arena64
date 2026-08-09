import { AdminShell } from "@/app/AdminShell";
import { useAdminSession } from "@/app/use-admin-session";
import { I18nProvider, useTranslation } from "@/shared/i18n";

/**
 * The admin console's root — A64-024.1 §6, §7.
 *
 * ## Every path through this component is a refusal except one
 *
 *     checking          a status message, and nothing privileged
 *     unauthenticated   sign in — the existing product auth UX
 *     forbidden         a plain refusal that names no role and no account
 *     unavailable       a retry, because the server said nothing usable
 *     authorized        the shell
 *
 * The shell is rendered in **one** branch, reached only by a
 * server-authoritative `200`. There is no way to enter it from local state,
 * and no state this app holds can be edited into one — which is what makes
 * a direct navigation and a refresh behave identically to a fresh sign-in.
 *
 * ## Why the refusal says so little
 *
 * `forbidden` tells the caller they lack access and stops. It does not name
 * the role they would need, does not say whether administrators exist, and
 * does not distinguish "not an administrator" from "account disabled" —
 * because the server does not either. A console that explained the
 * difference would be an oracle for anybody who reached it.
 */
function Gate() {
  const { t } = useTranslation();
  const { auth, recheck } = useAdminSession();

  if (auth.state === "authorized") {
    return <AdminShell session={auth.session} onSignedOut={recheck} />;
  }

  return (
    <main className="gate">
      <h1>{t("app.title")}</h1>
      {auth.state === "checking" && <p role="status">{t("auth.checking")}</p>}

      {auth.state === "unauthenticated" && (
        <>
          <p role="status">{t("auth.required")}</p>
          {/* The player client owns sign-in. This app has no login form of
              its own: a second credential surface would be a second thing
              to keep correct, and AD-04's separation is about session and
              origin rather than about duplicating authentication. */}
          <a className="action" href="/login">
            {t("auth.signIn")}
          </a>
        </>
      )}

      {auth.state === "forbidden" && (
        <>
          <p role="alert">{t("auth.denied")}</p>
          <p className="muted">{t("auth.deniedHint")}</p>
        </>
      )}

      {auth.state === "unavailable" && (
        <>
          <p role="alert">{t("auth.failed")}</p>
          <button type="button" className="action" onClick={recheck}>
            {t("auth.signIn")}
          </button>
        </>
      )}
    </main>
  );
}

export function App() {
  return (
    <I18nProvider>
      <Gate />
    </I18nProvider>
  );
}
