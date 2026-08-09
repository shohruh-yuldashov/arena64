import { useState } from "react";

import type { AdminSession } from "@/shared/api/admin-session";
import { useTranslation } from "@/shared/i18n";

/**
 * The admin shell — A64-024.1 §7.
 *
 * Architecture, not the final design. What it establishes is the frame a
 * later A64-024.x task fills: an identity header, a navigation container
 * whose sections are declared but **visibly not built**, and one landing
 * area.
 *
 * ## The sections are disabled, and they say so
 *
 * §7 permits placeholders "if they are clearly non-functional". Each is a
 * `<button disabled>` carrying the same localized "not built yet" label, so
 * a screen reader hears the state rather than inferring it from styling.
 * They are not links: a link that goes nowhere is a broken link, and a
 * disabled control is an honest one.
 *
 * ## Routing
 *
 * One section is reachable, so the "router" here is a `useState`. A real
 * router earns its place when there is a second destination and a URL worth
 * sharing — adding one now would be configuration around a single page,
 * and A64-024.2 is where it becomes real.
 */

/** Declared now so the shape is fixed; built later, one task at a time. */
const SECTIONS = [
  { id: "dashboard", label: "nav.dashboard", ready: true },
  { id: "users", label: "nav.users", ready: false },
  { id: "matches", label: "nav.matches", ready: false },
  { id: "tournaments", label: "nav.tournaments", ready: false },
  { id: "moderation", label: "nav.moderation", ready: false },
  { id: "notifications", label: "nav.notifications", ready: false },
  { id: "audit", label: "nav.audit", ready: false },
] as const;

export function AdminShell({
  session,
  onSignedOut,
}: {
  session: AdminSession;
  /** Re-asks the server after a sign-out, rather than assuming it worked. */
  onSignedOut: () => void;
}) {
  const { t } = useTranslation();
  const [active, setActive] = useState<string>("dashboard");

  const signOut = async () => {
    try {
      await fetch("/api/v1/auth/browser/logout", {
        method: "POST",
        credentials: "include",
      });
    } catch {
      /* Recheck below decides what actually happened. */
    }
    // Never assumes. If the request failed the server still holds the
    // session, and re-asking is what stops this app showing a signed-out
    // screen over a live one.
    onSignedOut();
  };

  return (
    <div className="shell">
      <header className="shell__header">
        <div>
          <h1>{t("app.title")}</h1>
          <p className="muted">{t("app.subtitle")}</p>
        </div>
        <div className="shell__identity">
          <span>
            {t("auth.signedInAs", { name: session.display_name ?? session.username })}
          </span>
          <button type="button" className="action" onClick={() => void signOut()}>
            {t("auth.signOut")}
          </button>
        </div>
      </header>

      <div className="shell__body">
        <nav aria-label={t("nav.label")}>
          <ul>
            {SECTIONS.map((section) => (
              <li key={section.id}>
                <button
                  type="button"
                  disabled={!section.ready}
                  // The active section is announced, not only styled —
                  // colour alone is never the signal.
                  aria-current={active === section.id ? "page" : undefined}
                  onClick={() => setActive(section.id)}
                >
                  {t(section.label)}
                  {!section.ready && <span className="muted"> · {t("nav.comingSoon")}</span>}
                </button>
              </li>
            ))}
          </ul>
        </nav>

        <main>
          <h2>{t("dashboard.title")}</h2>
          <p>{t("dashboard.empty")}</p>
          <p className="muted">{t("dashboard.emptyHint")}</p>
        </main>
      </div>
    </div>
  );
}
