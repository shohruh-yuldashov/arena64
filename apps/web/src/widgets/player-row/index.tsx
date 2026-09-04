import { Link } from "@tanstack/react-router";
import type { ReactNode } from "react";

import { initialsOf, nameOf, type PublicProfile } from "@/entities/profile";
import { useTranslation } from "@/shared/i18n";
import { formatDateTime } from "@/shared/lib/format";
import { Avatar, AvatarFallback, AvatarImage } from "@/shared/ui";

/**
 * One player in a dense list — A64-020.4 §4, §10.
 *
 * Shared by search, friends, both request lists and the blocked list, so a
 * row looks and behaves the same wherever it appears and presence is gated
 * in one place rather than five.
 *
 * ## `thumbnail_url`, not `avatar_url`
 *
 * A list of forty rows fetching forty full-size avatars is forty images
 * sized for a profile header. The thumbnail is what the API provides for
 * exactly this, and `avatar_url` is left to the larger surfaces.
 *
 * ## Presence: absent means absent
 *
 * `is_online` and `last_seen` are **omitted** by the API when privacy hides
 * them, so each is rendered only when present. Nothing here defaults an
 * absent `is_online` to "Offline" — that would publish a fact the owner
 * withheld — and nothing reads the deprecated `show_online_status` or
 * `show_last_seen` booleans, which are `true` only when their
 * audience-valued counterpart is `everyone` and would collapse a
 * friends-only setting into "off".
 *
 * Online state is never computed from a timestamp: `last_seen` five seconds
 * ago is not the same claim as "online", and inventing a "recently online"
 * category would be a product decision nobody made.
 *
 * The dot is decoration; the word beside it carries the meaning (WCAG
 * 1.4.1).
 */
export function PlayerRow({
  player,
  meta,
  actions,
}: {
  player: PublicProfile;
  /** A line under the name — "Friends since…", "Sent…". */
  meta?: string | null;
  actions?: ReactNode;
}) {
  const { t, locale } = useTranslation();
  const name = nameOf(player);

  return (
    // No border of its own — A64-025.8B §27. Every list in this product is
    // one card with ruled rows; five separate bordered boxes with gaps
    // between them was the shape social kept from before that existed.
    <li className="flex flex-wrap items-center gap-3 px-4 py-3 sm:flex-nowrap sm:px-5">
      <Link
        to="/players/$username"
        params={{ username: player.username }}
        // `basis-full` below `sm` — A64-025.8B §27. The row wraps there, but
        // `flex-1` still let the actions share the first line: two buttons
        // took 200 of 360 pixels and the identity kept what was left, so a
        // meta line reading "3+2 · Reytingli · 2 soat qoldi" lost 123 of
        // them. Taking the whole line pushes the actions to their own and
        // nothing has to be cut.
        className="focus-visible:ring-ring flex min-w-0 flex-1 basis-full items-center gap-3 rounded-md focus-visible:ring-2 focus-visible:outline-none sm:basis-auto"
        aria-label={`${t("social.actions.viewProfile")} — ${name}`}
      >
        <Avatar className="size-10 shrink-0">
          {player.thumbnail_url != null && <AvatarImage src={player.thumbnail_url} alt="" />}
          <AvatarFallback>{initialsOf(player)}</AvatarFallback>
        </Avatar>

        <span className="flex min-w-0 flex-col">
          <span className="truncate text-sm font-medium">{name}</span>
          {/* A64-025.8. Only when it says something the line above did not.
              `nameOf` falls back to the username when there is no display
              name, and most accounts have none — so this row rendered
              `alice` over `@alice` for the majority of players, two lines
              carrying one fact. */}
          {name !== player.username && (
            <span className="text-muted-foreground truncate text-xs">@{player.username}</span>
          )}

          {/* Each rendered only when the API sent it — see the note above. */}
          {player.is_online != null && (
            <span className="text-muted-foreground flex items-center gap-1 text-xs">
              <span
                aria-hidden="true"
                className={
                  player.is_online
                    ? "size-2 rounded-full bg-success"
                    : "bg-muted-foreground/50 size-2 rounded-full"
                }
              />
              {player.is_online ? t("social.presence.online") : t("social.presence.offline")}
            </span>
          )}
          {player.last_seen != null && (
            <span className="text-muted-foreground truncate text-xs">
              {t("social.presence.lastSeen", {
                when: formatDateTime(player.last_seen, locale) ?? "",
              })}
            </span>
          )}
          {meta != null && meta !== "" && (
            <span className="text-muted-foreground truncate text-xs">{meta}</span>
          )}
        </span>
      </Link>

      {actions != null && <div className="shrink-0">{actions}</div>}
    </li>
  );
}
