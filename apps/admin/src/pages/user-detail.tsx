import { Link, useParams } from "@tanstack/react-router";
import { useEffect, useState } from "react";

import { type AdminUserDetail, fetchUser } from "@/shared/api/client";
import { useTranslation } from "@/shared/i18n";

/**
 * One account — A64-024.3 §13.
 *
 * A real route rather than a modal, because the router already exists and
 * an operator investigating an account wants a URL they can send to a
 * colleague.
 *
 * **Only what the API returns.** No placeholder rows for data this phase
 * does not expose — a greyed "Rating: —" would imply the platform has an
 * answer it is withholding, when in fact the field is not on the response
 * at all (see the router on why rating summaries wait).
 *
 * Read-only, like the list: no action exists here while `admin.audit_entry`
 * is unbuilt.
 */
export function UserDetailPage() {
  const { t, locale } = useTranslation();
  const { userId } = useParams({ strict: false }) as { userId: string };

  const [user, setUser] = useState<AdminUserDetail | null>(null);
  const [state, setState] = useState<"loading" | "ready" | "error">("loading");

  useEffect(() => {
    const controller = new AbortController();
    setState("loading");

    void fetchUser(userId, controller.signal).then((outcome) => {
      if (controller.signal.aborted) return;
      if (outcome.status === "ok") {
        setUser(outcome.value);
        setState("ready");
        return;
      }
      setState("error");
    });

    return () => controller.abort();
  }, [userId]);

  return (
    <>
      <p>
        <Link to="/users">{t("users.back")}</Link>
      </p>

      {state === "loading" && <p role="status">{t("users.loading")}</p>}
      {state === "error" && (
        <p role="alert" className="error">
          {t("users.error")}
        </p>
      )}

      {state === "ready" && user !== null && (
        <>
          <h2>{user.display_name ?? user.username}</h2>

          <section>
            <h3>{t("users.sectionAccount")}</h3>
            <dl className="facts">
              <dt>{t("users.colUser")}</dt>
              <dd>{user.username}</dd>
              <dt>{t("users.colEmail")}</dt>
              <dd>{user.email}</dd>
              <dt>{t("users.colStatus")}</dt>
              {/* Text, not a colour — status must not be carried by hue
                  alone (§17). */}
              <dd>{t(user.is_active ? "users.active" : "users.inactive")}</dd>
              <dt>{t("users.colVerified")}</dt>
              <dd>{t(user.is_verified ? "users.verified" : "users.unverified")}</dd>
              <dt>{t("users.joined")}</dt>
              <dd>{new Date(user.created_at).toLocaleString(locale)}</dd>
              <dt>{t("users.userId")}</dt>
              <dd>
                <code>{user.id}</code>
              </dd>
            </dl>
          </section>

          <section>
            <h3>{t("users.sectionAdmin")}</h3>
            {user.is_admin ? (
              <dl className="facts">
                <dt>{t("users.colRole")}</dt>
                <dd>{t("users.roleAdmin")}</dd>
                {user.admin_role_granted_at !== null && (
                  <>
                    <dt>{t("users.grantedAt")}</dt>
                    <dd>{new Date(user.admin_role_granted_at).toLocaleString(locale)}</dd>
                  </>
                )}
              </dl>
            ) : (
              // No grant control here, deliberately — §7 forbids a
              // privilege-escalation button existing merely because a
              // Users page does. Roles are granted by operator command.
              <p className="muted">{t("users.notAnAdmin")}</p>
            )}
          </section>
        </>
      )}
    </>
  );
}
