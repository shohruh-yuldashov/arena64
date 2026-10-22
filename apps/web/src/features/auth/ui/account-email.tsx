import type { ReactNode } from "react";

import { useAccount } from "@/features/auth/model/account";
import { useTranslation } from "@/shared/i18n";
import { LoadFailure, SettingCard, SettingGroup, Spinner } from "@/shared/ui";

/**
 * The address this account signs in with, and whether it is verified —
 * A64-031.E.
 *
 * ## Only ever the owner's
 *
 * Read from `GET /auth/me`, which answers for the access token and takes no
 * identifier, so this component cannot be pointed at anybody else. Public
 * profiles, search, leaderboards and game pages are served by other
 * endpoints whose schemas carry no address at all —
 * `apps/api/tests/unit/test_email_exposure.py` holds that line.
 *
 * ## Stated, never inferred
 *
 * The status is the server's `is_verified` and nothing else. In particular
 * it is not derived from how the person signed in: an account created with
 * Google is verified because the server recorded it so, and this screen
 * says what the server says.
 *
 * Read-only on purpose. Changing the address, re-sending a code and
 * connecting sign-in methods are separate flows with their own rules, and a
 * button for any of them here would be a promise this checkpoint does not
 * keep.
 */
export function AccountEmail() {
  const { t } = useTranslation();
  const account = useAccount();

  return (
    <SettingGroup title={t("profile.account.title")} description={t("profile.account.private")}>
      <SettingCard>
        {account.isPending ? (
          <div className="flex justify-center px-4 py-6">
            <Spinner label={t("profile.account.loading")} />
          </div>
        ) : account.isError ? (
          <div className="px-4 py-4 sm:px-6">
            <LoadFailure
              message={t("profile.account.loadFailed")}
              onRetry={() => void account.refetch()}
            />
          </div>
        ) : (
          <dl className="divide-border divide-y">
            <Entry term={t("profile.account.email")}>
              {account.data.email ? (
                // `break-all`: an address is one unbroken token, and a long
                // one must wrap inside a 360px card rather than widen it.
                // `translate="no"`: a page translator must not rewrite it.
                <span className="break-all" translate="no">
                  {account.data.email}
                </span>
              ) : (
                <span className="text-muted-foreground">{t("profile.account.noEmail")}</span>
              )}
            </Entry>
            <Entry term={t("profile.account.verification")}>
              {account.data.is_verified
                ? t("profile.account.verified")
                : t("profile.account.notVerified")}
            </Entry>
          </dl>
        )}
      </SettingCard>
    </SettingGroup>
  );
}

/**
 * One label and its value. A `<dt>`/`<dd>` pair rather than `SettingRow`:
 * that row names a *control* with a `<label>`, and a value nobody can
 * change is not one.
 */
function Entry({ term, children }: { term: string; children: ReactNode }) {
  return (
    <div className="flex flex-col gap-1 px-4 py-4 sm:flex-row sm:items-baseline sm:justify-between sm:gap-8 sm:px-6">
      <dt className="text-sm font-medium">{term}</dt>
      <dd className="min-w-0 text-sm sm:text-right">{children}</dd>
    </div>
  );
}
