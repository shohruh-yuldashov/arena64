import { NotificationList } from "@/features/notifications/ui/notification-list";
import { useTranslation } from "@/shared/i18n";

/**
 * `/notifications` — what happened while you were away. A64-021.1 §18.
 *
 * The page owns the heading and the width and nothing else:
 * `NotificationList` holds the queries, the states and the mutations, in
 * the same arrangement every other page on this platform makes.
 *
 * Bounded at `max-w-2xl` rather than the shell's `max-w-5xl`: a
 * notification is one line of text, and a full-width row puts the timestamp
 * a screen away from the sentence it belongs to on a desktop (§29).
 */
export default function NotificationsPage() {
  const { t } = useTranslation();

  return (
    <section className="mx-auto flex w-full max-w-2xl flex-col gap-6">
      {/* No subtitle. The first attempt borrowed `emptyDescription` — "New
          notifications will appear here" — which is a sentence written for
          an empty list and reads as nonsense above a full one. A heading
          with nothing useful to add under it is better than a heading with
          the wrong thing. */}
      <h1 className="text-2xl font-semibold tracking-tight">{t("notifications.title")}</h1>
      <NotificationList />
    </section>
  );
}
