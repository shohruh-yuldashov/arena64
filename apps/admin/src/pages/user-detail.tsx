import { Link, useParams } from "@tanstack/react-router";
import { useEffect, useState } from "react";

import { CATEGORY_LABELS, ModerationActions } from "@/features/moderation/moderation-actions";
import {
  type AdminSanction,
  type AdminUserDetail,
  fetchUser,
  type ModerationCategory,
} from "@/shared/api/client";
import { type TranslationKey, useTranslation } from "@/shared/i18n";
import { ErrorNotice } from "@/shared/ui/error-notice";

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
 * **The one page with actions** — A64-024.6. Restricting an account belongs
 * here rather than on a list, because an operator should have read who
 * somebody is before withholding their access. Both actions confirm, and
 * both state their consequence.
 */
export function UserDetailPage() {
  const { t, locale } = useTranslation();
  const { userId } = useParams({ strict: false }) as { userId: string };

  const [user, setUser] = useState<AdminUserDetail | null>(null);
  const [state, setState] = useState<"loading" | "ready" | "error">("loading");
  const [notice, setNotice] = useState<TranslationKey | null>(null);

  /**
   * Folds the server's answer back into the page.
   *
   * The response **is** the new state — it carries `is_effective` computed
   * against the server's clock — so nothing is guessed locally and no
   * second request is needed. A refetch here would show a stale page for
   * as long as it took, which is exactly when an operator looks.
   */
  const applyChange = (sanction: AdminSanction) => {
    setUser((current) =>
      current === null
        ? current
        : {
            ...current,
            moderation: sanction.is_effective
              ? { is_restricted: true, restriction: sanction }
              : { is_restricted: false, restriction: null },
          },
    );
    setNotice(sanction.is_effective ? "moderation.doneRestricted" : "moderation.doneRestored");
  };

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

  const restriction = user?.moderation.restriction ?? null;

  const reasonOf = (sanction: AdminSanction) => {
    const label = CATEGORY_LABELS[sanction.case.category as ModerationCategory];
    return label === undefined ? sanction.case.category : t(label);
  };

  return (
    <>
      <p>
        <Link to="/users">{t("users.back")}</Link>
      </p>

      {state === "loading" && <p role="status">{t("users.loading")}</p>}
      {state === "error" && <ErrorNotice message={t("users.error")} />}

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

          <section>
            <h3>{t("moderation.section")}</h3>
            {/* Status as text, never colour alone — §27. */}
            <dl className="facts">
              <dt>{t("moderation.colStatus")}</dt>
              <dd>
                {t(
                  user.moderation.is_restricted
                    ? "moderation.restricted"
                    : "moderation.notRestricted",
                )}
              </dd>
              {restriction !== null && (
                <>
                  <dt>{t("moderation.reason")}</dt>
                  <dd>{reasonOf(restriction)}</dd>
                  <dt>{t("moderation.since")}</dt>
                  <dd>{new Date(restriction.starts_at).toLocaleString(locale)}</dd>
                  <dt>{t("moderation.expires")}</dt>
                  <dd>
                    {restriction.expires_at === null
                      ? t("moderation.indefinite")
                      : new Date(restriction.expires_at).toLocaleString(locale)}
                  </dd>
                  <dt>{t("moderation.decidedBy")}</dt>
                  <dd>{restriction.case.opened_by_username ?? restriction.case.opened_by}</dd>
                  <dt>{t("moderation.note")}</dt>
                  <dd>{restriction.case.reasoning}</dd>
                </>
              )}
            </dl>

            {/* A64-024 hardening §7. The one deep link this page was
                missing, and the destination declares the parameter:
                `/matches`'s `validateSearch` takes `participant`. No
                guessed query strings — a link the destination ignores is a
                filter an operator believes is applied. */}
            <p className="detail-links">
              <Link to="/matches" search={{ participant: user.id }}>
                {t("users.viewMatches")}
              </Link>
              {/* The restrictions list, not this account's restriction —
                  the effective one is already above. This is where an
                  operator goes to see the whole picture, and it is the
                  console that owns the history. */}
              <Link to="/moderation">{t("users.viewModeration")}</Link>
            </p>

            <ModerationActions
              userId={user.id}
              displayName={user.display_name ?? user.username}
              isRestricted={user.moderation.is_restricted}
              onChanged={applyChange}
            />

            {notice !== null && (
              <p role="status" className="notice">
                {t(notice)}
              </p>
            )}
          </section>
        </>
      )}
    </>
  );
}
