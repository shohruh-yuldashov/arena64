# Public Content and Discovery

| Field            | Value                                         |
| ---------------- | --------------------------------------------- |
| **Status**       | Implemented                                   |
| **Owner**        | Shohruh                                       |
| **Applies to**   | `apps/web`, `apps/api`                        |
| **Decided in**   | A64-026.4 — `specs/product-experience.md` §43 |
| **Last updated** | 2026-09-05                                    |

---

## 1. What this covers

Which surfaces a person with no Arena64 account can reach, what a link to
one does when it arrives somewhere else, and which of them a search engine
may list. The indexing column is owned by `docs/04-frontend/seo.md` and
repeated here only as a cross-reference, never as a second source.

## 2. Three decisions, not one

They are routinely collapsed into a single "is it public?" and they are not
the same question:

| Term          | Means                                                             |
| ------------- | ----------------------------------------------------------------- |
| **Public**    | The server answers a caller with no token                         |
| **Shareable** | A URL handed to somebody else opens the thing, not a sign-in form |
| **Indexable** | A search engine may crawl and list it                             |

A surface can be any combination. Tournaments are public **and** shareable
**and not** indexable, and that is a deliberate position rather than an
unfinished one — §5 says why.

## 3. The matrix

`API` is whether the HTTP endpoint answers without a token; `Page` is
whether the route renders without a session.

| Surface                          | API     | Page    | Shareable | Indexable | Sitemap | Privacy control                    |
| -------------------------------- | ------- | ------- | --------- | --------- | ------- | ---------------------------------- |
| Landing `/`                      | n/a     | **Yes** | **Yes**   | **Yes**   | **Yes** | None — it is marketing copy        |
| Tournament lobby `/tournaments`  | **Yes** | **Yes** | **Yes**   | No (§5)   | No      | `DRAFT` is hidden from anonymous   |
| Tournament `/tournaments/{id}`   | **Yes** | **Yes** | **Yes**   | No (§5)   | No      | `DRAFT` answers `404`              |
| Bracket, standings               | **Yes** | **Yes** | **Yes**   | No (§5)   | No      | Follows its tournament             |
| Own entry `/…/registrations/me`  | No      | n/a     | n/a       | n/a       | n/a     | It is a question about the viewer  |
| Enter / withdraw                 | No      | No      | n/a       | n/a       | n/a     | `VerifiedUser`                     |
| Player profile `/players/{name}` | **Yes** | **Yes** | **Yes**   | No        | No      | The player's own privacy settings  |
| Player search `/search`          | No      | No      | n/a       | No        | n/a     | Enumeration over people (§4)       |
| Live game `/games/{id}`          | No      | No      | No        | No        | n/a     | Two players, realtime channel (§4) |
| Replay `/games/{id}/replay`      | No      | No      | No        | No        | n/a     | Two players' moves (§4)            |
| Match history                    | No      | No      | No        | No        | n/a     | Behind the account it belongs to   |
| Friends, requests, blocked       | No      | No      | No        | No        | n/a     | A player's social graph            |
| Challenges                       | No      | No      | No        | No        | n/a     | Addressed to one player            |
| Notifications                    | No      | No      | No        | No        | n/a     | Addressed to one player            |
| Settings, own profile            | No      | No      | No        | No        | n/a     | The account itself                 |
| Leaderboards, player directory   | —       | —       | —         | —         | —       | **Do not exist** (§6)              |

## 4. Why the closed rows are closed

A surface was opened when the backend already treats every viewer alike, and
left closed when opening it would require **deciding** something about
people that no spec has decided.

- **Games, live and replayed.** Two players' identities and their moves.
  `specs/game` states no public-visibility rule, so opening one would be
  inventing a rule rather than implementing one. The live case adds a
  realtime channel whose authorization is per-subscriber.
- **Player search.** Enumeration over people rather than over events. The
  `profiles` module already rate-limits it behind a token for exactly that
  reason, and removing the token removes the subject the limit counts.
- **Everything addressed to one player** — history, friends, challenges,
  notifications, settings. There is no viewer-independent version of these.

## 5. Public and shareable, and still not indexed

Tournaments are the row this section exists for, because the `Disallow` in
`robots.txt` looks like something to clean up now that the router shows an
open route. It is not, and `src/app/seo.test.ts` asserts it by name.

Every route in this SPA serves the same `index.html`: one title, one
description, and a body of zero characters — measured in A64-026.3 and
recorded in `docs/04-frontend/seo.md` §6. An indexed tournament would enter
a search index as a copy of the landing page's title, describing none of
them and diluting the one page that is meant to be found.

**The blocker is metadata, not visibility.** When there is a per-route
metadata layer — which needs the hydration path recorded as an ADR-sized
decision — completed tournaments are the natural first thing to open, and
`robots.txt` is the one line to change.

The same reasoning is why a shared tournament shows the site's preview card
rather than its own. A link preview is built by a crawler that does not
execute JavaScript.

## 6. Not built, and not because it was forgotten

| Asked for           | Status                                                                                                                              |
| ------------------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| Public leaderboards | **No such feature.** Ratings exist per player; a ranking of players does not, and inventing one is product work, not discovery work |
| Player directory    | Same. A list of everybody is the enumeration §4 declines                                                                            |
| Public game replays | Deferred with a reason (§4), not skipped                                                                                            |
| Per-tournament OG   | Cannot work client-side (§5)                                                                                                        |

No placeholder content was created for any of them. A directory of invented
players would be worse than an absent one.

## 7. Rate limits

Opening a read removes the subject a limit counts. The three tournament
reads carry one shared IP-scoped bucket, configured by
`tournament_read_ip_limit` and `tournament_read_window_seconds`, and the
reasoning — why one bucket, why IP, why the mutations are not limited —
lives with the policy in
`apps/api/app/modules/tournament/presentation/rate_limits.py`.

## Related

- `specs/product-experience.md` §43 — the decisions and the measurements
- `docs/04-frontend/seo.md` — the indexing policy this cross-references
- `specs/tournament` — the module's own visibility rules
