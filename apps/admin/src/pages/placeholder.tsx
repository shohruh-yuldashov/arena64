import { useTranslation } from "@/shared/i18n";

/**
 * A declared but unbuilt section — A64-024.2 §6, §20.
 *
 * Routes for Users, Matches, Tournaments, Moderation, Notifications and
 * the Audit log exist so the shape is fixed and the guard covers them from
 * the start. What each *does* is a later task, and saying so in the
 * console is more honest than a route that 404s or a nav item that does
 * nothing.
 *
 * Protected exactly like every other admin page: the placeholder is behind
 * the same guard, so adding real content later changes the body and not
 * the boundary.
 */
export function PlaceholderPage({
  titleKey,
}: {
  titleKey: Parameters<ReturnType<typeof useTranslation>["t"]>[0];
}) {
  const { t } = useTranslation();
  return (
    <>
      <h2>{t(titleKey)}</h2>
      <p>{t("route.placeholder")}</p>
      <p className="muted">{t("route.placeholderHint")}</p>
    </>
  );
}
