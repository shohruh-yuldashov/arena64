import { Link } from "@tanstack/react-router";

import type { Notification } from "@/features/notifications/api";
import { notificationHref } from "@/features/notifications/model/navigation";
import {
  notificationMessage,
  notificationSubject,
} from "@/features/notifications/model/render";
import { useTranslation } from "@/shared/i18n";
import { formatDateTime } from "@/shared/lib/format";
import { Avatar, AvatarFallback, AvatarImage } from "@/shared/ui";

/**
 * One notification — A64-021.1 §20, §28, §29.
 *
 * ## The backend supplies facts; this supplies the sentence
 *
 * The row renders a **translated** message assembled from a `type` and an
 * actor, never a string the server composed. That is what makes the same
 * notification readable in uz, ru and en, and it is why nothing here can
 * inject markup: there is no server-supplied string that reaches the DOM as
 * anything but text content.
 *
 * A `type` this build does not know renders the generic sentence rather than
 * a blank row or a key. A notification that arrives from a newer backend is
 * still a notification worth seeing.
 *
 * A64-021.4 moved the type-to-sentence and type-to-avatar decisions into
 * `model/render`, because six types made them the larger half of this file
 * and neither is a rendering concern — this component now takes a message
 * and a subject and lays them out.
 *
 * ## Unread is not a colour
 *
 * §28: the unread state is a dot **and** a text label that only a screen
 * reader reads, so it survives greyscale, colour blindness and a high
 * contrast theme. The bolder text is a third signal rather than the signal.
 *
 * ## Why the whole row is one link
 *
 * A target is one destination, and a card with a separate "open" control
 * would put two tab stops where a player expects one. When there is no
 * target — an unknown type, or a notification that names no destination —
 * the row renders as a plain element rather than as a link that goes
 * nowhere (§6).
 */
export function NotificationRow({
  notification,
  onOpen,
}: {
  notification: Notification;
  onOpen: (notification: Notification) => void;
}) {
  const { t, locale } = useTranslation();

  // Both come from `model/render`, which is where the type-to-sentence and
  // type-to-avatar decisions live — A64-021.4 §20. The row renders what it
  // is handed and chooses nothing.
  const { key, values } = notificationMessage(notification);
  const subject = notificationSubject(notification);
  const message = t(key, values);
  const when = formatDateTime(notification.created_at, locale) ?? "";
  const href = notificationHref(notification.target);

  const body = (
    <>
      <Avatar className="size-9 shrink-0">
        {subject.thumbnailUrl !== null && <AvatarImage src={subject.thumbnailUrl} alt="" />}
        {/* `aria-hidden`, because the subject's name is already in the
            message beside it — announcing initials as well would read the
            same person twice. A tournament has no picture at all and falls
            back to its own initials, which is why the label is a subject
            property rather than an actor field. */}
        <AvatarFallback aria-hidden="true">
          {subject.label.slice(0, 2).toUpperCase()}
        </AvatarFallback>
      </Avatar>

      <div className="min-w-0 flex-1">
        <p className={notification.is_read ? "text-sm" : "text-sm font-medium"}>{message}</p>
        <p className="text-muted-foreground mt-0.5 text-xs">
          {/* `<time>` with a machine-readable `dateTime`, and human text
              inside it — §23, §28. The server's instant is authoritative;
              `Intl` decides how it reads in this locale. */}
          <time dateTime={notification.created_at}>{when}</time>
        </p>
      </div>

      {!notification.is_read && (
        <span className="flex shrink-0 items-center gap-1.5">
          {/* The visible dot carries no information a screen reader can
              use, so the label beside it does — and it is `sr-only` rather
              than a tooltip, which a keyboard user never sees. */}
          <span aria-hidden="true" className="bg-primary size-2 rounded-full" />
          <span className="sr-only">{t("notifications.unread")}</span>
        </span>
      )}
    </>
  );

  // `min-h-11` on both branches: 44px is the touch target §28 requires, and
  // a row that is only as tall as its text would miss it on a phone.
  const className =
    "focus-visible:ring-ring flex min-h-11 w-full items-start gap-3 rounded-md p-3 text-left focus-visible:ring-2 focus-visible:outline-none";

  if (href === null) {
    return (
      <li>
        <div className={className}>{body}</div>
      </li>
    );
  }

  return (
    <li>
      <Link
        to={href}
        className={`${className} hover:bg-muted`}
        // Marking read is fired here and **not awaited**: navigation must
        // not wait on a mutation, or a notification opened on a bad
        // connection is a tap that appears to do nothing (§21).
        onClick={() => onOpen(notification)}
      >
        {body}
      </Link>
    </li>
  );
}
