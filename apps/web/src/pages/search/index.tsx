import { useEffect, useId, useState } from "react";

import { MIN_QUERY_LENGTH } from "@/features/social/api";
import { useUserSearch } from "@/features/social/model/queries";
import { RelationshipActions } from "@/features/social/ui/relationship-actions";
import { useTranslation } from "@/shared/i18n";
import { ListState } from "@/shared/ui";
import { Button, Input, Spinner } from "@/shared/ui";
import { PlayerRow } from "@/widgets/player-row";
import { SocialNav } from "@/widgets/social-nav";

/**
 * `/search` — find players.
 *
 * ## Debounced, and not merely throttled
 *
 * The input is local state and the *query* is a debounced copy of it, so
 * typing "player" issues one request rather than six. 300 ms is long enough
 * to swallow a burst of keystrokes and short enough that a deliberate pause
 * feels like a search rather than a delay.
 *
 * Below `MIN_QUERY_LENGTH` no request is made at all — the API's own floor
 * is two characters, and asking for one is a round trip that can only 422.
 *
 * ## The client filters nothing
 *
 * Search already excludes the caller and everybody in either direction of a
 * block, on the server. Re-filtering here would be a second implementation
 * of an exclusion rule, and the copy is what goes stale.
 *
 * ## Announced
 *
 * The result count is an `aria-live` region, so a screen-reader user learns
 * that results arrived without having to go looking for them.
 */
const DEBOUNCE_MS = 300;

export default function SearchPage() {
  const { t } = useTranslation();
  const inputId = useId();
  const [typed, setTyped] = useState("");
  const [query, setQuery] = useState("");

  useEffect(() => {
    const timer = setTimeout(() => setQuery(typed), DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [typed]);

  const search = useUserSearch(query);
  const results = search.data?.pages.flatMap((page) => page.items) ?? [];
  const tooShort = query.trim().length < MIN_QUERY_LENGTH;

  return (
    <SocialNav title={t("social.search.title")} description={t("social.search.hint")}>
      <div className="flex flex-col gap-4">
        <div className="flex flex-col gap-1.5">
          <label htmlFor={inputId} className="text-sm font-medium">
            {t("social.search.label")}
          </label>
          <Input
            id={inputId}
            type="search"
            autoComplete="off"
            className="min-h-11"
            placeholder={t("social.search.placeholder", { min: MIN_QUERY_LENGTH })}
            aria-describedby={`${inputId}-status`}
            value={typed}
            onChange={(event) => setTyped(event.target.value)}
          />
        </div>

        {/* One live region for every state, so a screen reader hears the
            transition rather than three regions competing. */}
        <p
          id={`${inputId}-status`}
          role="status"
          aria-live="polite"
          className="text-muted-foreground text-sm"
        >
          {tooShort
            ? t("social.search.tooShort", { min: MIN_QUERY_LENGTH })
            : search.isFetching
              ? t("social.search.searching")
              : t("social.search.resultCount", { count: results.length })}
        </p>

        {!tooShort && (
          <ListState
            isPending={search.isPending}
            isError={search.isError}
            isEmpty={results.length === 0}
            emptyTitle={t("social.search.noResults")}
            onRetry={() => void search.refetch()}
          >
            <ul aria-label={t("social.search.title")} className="flex flex-col gap-2">
              {results.map((player) => (
                <PlayerRow
                  key={player.id}
                  player={player}
                  actions={
                    <RelationshipActions
                      playerId={player.id}
                      playerName={player.display_name ?? player.username}
                      state={player.relationship}
                    />
                  }
                />
              ))}
            </ul>
          </ListState>
        )}

        {search.hasNextPage && (
          <Button
            variant="outline"
            className="min-h-11 self-start"
            disabled={search.isFetchingNextPage}
            onClick={() => void search.fetchNextPage()}
          >
            {search.isFetchingNextPage ? (
              <Spinner label={t("social.search.searching")} />
            ) : (
              t("social.search.loadMore")
            )}
          </Button>
        )}
      </div>
    </SocialNav>
  );
}
