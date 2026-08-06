import { Link } from "@tanstack/react-router";
import { BellIcon } from "lucide-react";

import { isAuthenticated } from "@/entities/session";
import { useSession } from "@/features/auth/model/session-provider";
import { useUnreadCount } from "@/features/notifications/model/queries";
import { useTranslation } from "@/shared/i18n";
import { Button } from "@/shared/ui";

/**
 * The way into `/notifications`, and the unread badge — A64-021.1 §18, §28.
 *
 * ## Why the header and not the session menu
 *
 * A notification badge is a *state* a player checks at a glance, and the
 * session menu is a row of destinations. Putting it in the header beside
 * them keeps it visible without opening anything — which is the whole point
 * of a badge — and costs one control rather than a redesign (§18).
 *
 * ## The count has a textual equivalent, always
 *
 * §18 requires it and §28 spells it out: the number is drawn in a badge that
 * is `aria-hidden`, and the button's accessible name carries the same fact
 * in words. A screen reader hears "Notifications — 3 unread"; a sighted
 * player sees a 3. Neither depends on the other, and the state is never
 * carried by colour alone.
 *
 * ## A failed count renders nothing rather than something wrong
 *
 * `useUnreadCount` does not retry and this shows no badge when it has no
 * answer (§22). A permanent red dot because one request failed on a train
 * is worse than no dot: it teaches a player to ignore the badge.
 *
 * ## Signed out, it is absent
 *
 * Not disabled and not empty — absent. There is no anonymous notification
 * list to link to, and a control that only ever says "sign in first" is a
 * control that should not be there.
 */
export function NotificationBell() {
  const { t } = useTranslation();
  const { state } = useSession();
  const unread = useUnreadCount();

  if (!isAuthenticated(state)) return null;

  const count = unread.data?.unread_count ?? 0;
  const label =
    count > 0 ? t("notifications.bellWithUnread", { count }) : t("notifications.bell");

  return (
    <Button asChild size="sm" variant="ghost" className="relative min-h-11">
      <Link to="/notifications" aria-label={label}>
        <BellIcon aria-hidden="true" className="size-4" />
        {count > 0 && (
          // `aria-hidden`, because the button's own label already says the
          // same thing in words — announcing both would read the count
          // twice. Capped at 99+ so a long-absent player's badge does not
          // widen the header.
          <span
            aria-hidden="true"
            className="bg-primary text-primary-foreground absolute -top-0.5 -right-0.5 min-w-4 rounded-full px-1 text-[10px] leading-4 font-medium"
          >
            {count > 99 ? "99+" : count}
          </span>
        )}
      </Link>
    </Button>
  );
}
