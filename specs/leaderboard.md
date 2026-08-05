# Feature Specification — Leaderboard

| Field | Value |
| --- | --- |
| **Spec ID** | `SPEC-LEADERBOARD` |
| **Status** | Approved |
| **Owner** | _Unassigned_ |
| **Created** | 2026-08-05 |
| **Last updated** | 2026-08-05 |
| **Related ADRs** | [`ADR-001`](../docs/07-decisions/ADR-001-glicko2-incremental.md) |
| **Related specs** | [`rating.md`](./rating.md) |

---

## 1. Summary

Global standings for one **rating key** — `(ProductVariant, SpeedClass)` — ordered by rating,
paginated by cursor, and read directly from `rating.player_rating`.

## 2. Scope

| In | Out |
| --- | --- |
| One global ladder per key | Regional ladders |
| Cursor pagination | Seasonal ladders and resets |
| Provisional players, marked | A minimum-games threshold |
| Read-only access | Any write, rebuild or invalidation surface |

## 3. Ordering

```
rating DESC, deviation ASC, player_id ASC
```

**Total**, and that is a correctness property rather than tidiness: `player_id` is unique, so no
two rows compare equal, so a cursor cannot skip a player or show one twice. Two keys would leave
ties in whatever order the heap happened to hold — stable enough to pass a small test by luck and
not an order at all.

Deviation ascending is second **deliberately**: between two players on the same rating, the one
the platform is more sure about ranks higher. Ordering by games played instead would rank a
grinder above a stronger player, which is a different product decision nobody has made.

Served by `ix_player_rating__standings`, whose column order is this `ORDER BY` exactly.

## 4. Pagination

**Keyset, not offset.** The cursor carries the last row's three ordering values and the next page
is "strictly after this tuple".

`OFFSET` re-scans and skips, so it costs more the deeper a reader goes — and, worse, it *shifts*
when a rating moves between page reads, so a player can be seen twice or missed entirely. On a
live ladder that is not a performance question, it is a correctness one.

`MAX_PAGE_SIZE = 200`, `DEFAULT_PAGE_SIZE = 50`. A page asks for `limit + 1` rows so that a full
page which is also the last one does not send a reader back for an empty one.

## 5. Provisional players

**Shown, and marked.** No minimum-games threshold. `is_provisional` is derived from
`games_played < 25` on read, never stored — a stored flag is a second copy of what the counter
already says, and the copy is what goes stale.

A ladder that hid its newcomers would be one nobody new can see themselves on.

## 6. Why there is no projection table and no Redis copy

**The leaderboard is a query over `rating.player_rating`.** Not a second table, not a sorted set,
and not a consumer of `rating.updated`.

A64-017.4 asked for a projection updated on every rating change. What was built instead reads the
source relation, and the reason is that task's own constraint — *"do not duplicate the source of
truth"*:

| Option | Consistency | Cost |
| --- | --- | --- |
| **Derived query** *(built)* | **None to reason about** — the read is the same rows the rating write updated | One index scan per page |
| Projection table fed by `rating.updated` | Behind the source by however long the relay took | A second write path, a rebuild job, and a second answer to "what does this player rate" |
| Redis sorted set | Same, plus eviction | All of the above, plus `caching.md` C-5 — *"a key is never the sole record of anything competitive"* |

The requirement's intent was *immediate* updates and no scheduled rebuilds. A derived read is the
strongest form of both: there is no window at all, because there is nothing to propagate.

### 6.1 When this stops being right

A measurement, not a guess. A page is one index scan today. If the relation grows to where that
scan is the bottleneck:

1. Make `ix_player_rating__standings` covering, so the read never touches the heap.
2. Only then, a Redis sorted set per key fed by `rating.updated`.

At step 2 the questions the task asked get answered with a number behind them:

| Question | Answer, when it exists |
| --- | --- |
| Ownership | `rating` — sole writer, as `caching.md` C-8 requires |
| Keyspace | `lb:v1:<variant>:<speed_class>`, one sorted set per key |
| Invalidation | None. `ZADD` on each `rating.updated`; the score *is* the rating |
| Rebuild | Full scan of `rating.player_rating` into a fresh key, then rename |
| Consistency | Eventually consistent, bounded by relay lag. PostgreSQL stays authoritative — a lost key costs a rebuild, never a standing |

## 7. Public API

`rating.public.LeaderboardReader`, read-only. No write, no rebuild, no invalidation: there is
nothing to rebuild, and a surface that offered one would imply a second copy exists.

| Method | Answers |
| --- | --- |
| `page(key, *, after, limit)` | One page of `key`'s ladder, best first |
| `around(player_id, *, key, span)` | This player's rank and the rows either side; `None` when they have no row in `key` |

Entries carry the player id and their rating only. No handle, no avatar, no country — those are
`profiles`' and are composed by whoever renders the page. A leaderboard entry that carried them
would make `rating` depend on a module it has no business knowing about, and make every ranking
read a join.

### 7.1 Rank — derived, never stored

`around`'s `rank` is *how many rows sort strictly above this one, plus one*, computed at read
time. A rank is a property of the whole relation rather than of a row, so storing one would make
every rating update rewrite an unbounded number of rows — and a stale rank is worse than none.

Ranks are **unique**: §3's ordering is total, so no two players share a position. Deliberately
unlike a tournament's placement, where a shared tier is the product rule
([`tournament.md`](./tournament.md) §6g).

### 7.2 HTTP surface — A64-020.0A

Authenticated, like every route outside `/health`. A rating is public to *every player*, which is
not the same as public to the internet.

| Route | Answers |
| --- | --- |
| `GET /api/v1/leaderboard` | One key's ladder, keyset-paged. `variant`, `speed_class`, `after`, `limit` |
| `GET /api/v1/leaderboard/around/{player_id}` | Rank and neighbours. `span` ≤ 25, default 5 |

`next_cursor` is **opaque** — base64 over the three ordering values, sent back unread. Publishing
them as fields would make §3's ordering a contract that cannot change without breaking clients.
Encoded, not encrypted: it carries nothing a caller could not read in the page it came from. Every
way a cursor can be malformed collapses to one `422 invalid_cursor`, because a caller can do
nothing differently for any of them and distinguishing them would narrate the encoding to whoever
is probing it.

`around` answers **`404`** for a player with no row in that key — they are not on this ladder, and
there is no position to return. That is deliberately different from `GET /players/{id}/ratings`
([`rating.md`](./rating.md) §14.1), which answers every id: a rating exists for everybody, a
*ranking* only for a player with a stored row.

**Cost is flat**, measured rather than assumed: one statement per page whatever the limit, four
per `around` whatever the span.

## 8. Open questions

| # | Question | Blocked work |
| --- | --- | --- |
| OQ-1 | Are regional or friends-scoped ladders wanted? (`domain-model.md` Q-18) | Any scope beyond global |
| OQ-2 | Do seasons reset standings? | Seasonal ladders — `specs/rating.md` OQ-3 |
| OQ-3 | Should a frozen rating appear on the ladder? | Today it does; whether a player under investigation should be publicly ranked is a product call |
