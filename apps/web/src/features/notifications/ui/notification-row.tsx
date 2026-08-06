import { Link } from "@tanstack/react-router";

import type { Notification } from "@/features/notifications/api";
import { notificationHref } from "@/features/notifications/model/navigation";
import { type TranslationKey, useTranslation } from "@/shared/i18n";
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

  // The same fallback `entities/user.displayNameOf` applies, spelled out
  // because that helper takes a whole `UserRead` and an actor is a snapshot
  // of three fields — widening the helper to accept both would make it
  // accept anything with a `username`.
  const actorName = notification.actor.display_name ?? notification.actor.username;
  const message = t(messageKeyOf(notification.type), { actor: actorName });
  const when = formatDateTime(notification.created_at, locale) ?? "";
  const href = notificationHref(notification.target);

  const body = (
    <>
      <Avatar className="size-9 shrink-0">
        {notification.actor.thumbnail_url !== null && (
          <AvatarImage src={notification.actor.thumbnail_url} alt="" />
        )}
        <AvatarFallback aria-hidden="true">
          {actorName.slice(0, 2).toUpperCase()}
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

/**
 * The message key for a type, or the generic one.
 *
 * A closed mapping rather than a template built from the type string: a
 * server that sent `types.<anything>` would otherwise choose a translation
 * key, and a missing key renders as itself.
 */
function messageKeyOf(type: string): TranslationKey {
  switch (type) {
    case "friend_request_received":
      return "notifications.types.friend_request_received";
    case "friend_request_accepted":
      return "notifications.types.friend_request_accepted";
    default:
      return "notifications.types.unknown";
  }
}
