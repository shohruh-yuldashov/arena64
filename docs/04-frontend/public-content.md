# Public Content and Discovery

| Field            | Value                                                 |
| ---------------- | ----------------------------------------------------- |
| **Status**       | Implemented                                           |
| **Owner**        | Shohruh                                               |
| **Applies to**   | `apps/web`, `apps/api`                                |
| **Decided in**   | A64-026.4 §43, audited and corrected in A64-026.5 §44 |
| **Last updated** | 2026-09-05                                            |

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
| Leaderboards, player directory   | —       | —       | —         | —         | —       | **No surface exists** (§6)         |

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

| Asked for           | Status                                                                                                                                                                                                                                                                                                                                                                                                      |
| ------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Public leaderboards | **No leaderboard surface exists**, corrected in A64-026.5. The API _does_ have `GET /leaderboard`, behind `CurrentUser`, and nothing in `apps/web` reads it — no route, no page, no query. There is no public leaderboard because there is no leaderboard a player can reach at all, and building one is product work rather than discovery work. A64-026.4's "no such feature" was wrong about the backend |
| Player directory    | Same. A list of everybody is the enumeration §4 declines                                                                                                                                                                                                                                                                                                                                                    |
| Public game replays | Deferred with a reason (§4), not skipped                                                                                                                                                                                                                                                                                                                                                                    |
| Per-tournament OG   | Cannot work client-side (§5)                                                                                                                                                                                                                                                                                                                                                                                |

No placeholder content was created for any of them. A directory of invented
players would be worse than an absent one.

## 7. What the product may claim

A64-026.5 found the auth pages describing Arena64 as offering
"leaderboards" — in all three languages, under the wordmark, on sign-in,
registration, verification and both password screens. §6 is why that was
false.

`src/shared/i18n/i18n.test.tsx` now asserts by name that no catalogue makes
the claim. Copy that describes a feature is the kind that outlives the
feature's absence, and a marketing sentence is the last place anybody looks
for a product inventory.

The same string called the game "checkers" where the rest of the English
catalogue says "draughts" — one product, two names, on the page a visitor
reaches straight from the landing page's primary call to action. Also
asserted.

## 8. Rate limits

Opening a read removes the subject a limit counts. The three tournament
reads carry one shared IP-scoped bucket, configured by
`tournament_read_ip_limit` and `tournament_read_window_seconds`, and the
reasoning — why one bucket, why IP, why the mutations are not limited —
lives with the policy in
`apps/api/app/modules/tournament/presentation/rate_limits.py`.

## Related

- `specs/product-experience.md` §43 — the decisions and the measurements
- `specs/product-experience.md` §44 — the closing audit of the A64-026 epic
- `docs/04-frontend/seo.md` — the indexing policy this cross-references
- `specs/tournament` — the module's own visibility rules

## 9. A64-026 epic status

Closed on 2026-09-05 by the A64-026.5 audit. What "closed" means here is
narrow and worth stating: the public experience can go to production without
being redesigned. It does not mean nothing is left.

### What must happen at deploy time, and cannot happen in this repository

| #   | Requirement                                                                                    | Owner      | Fails how                                                                                                                                                                                     |
| --- | ---------------------------------------------------------------------------------------------- | ---------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | `VITE_PUBLIC_ORIGIN=https://arena64.gg` at build time, equal to the backend's `PUBLIC_APP_URL` | Deployment | **Fails closed.** Without it `robots.txt` becomes `Disallow: /` and no canonical, sitemap or structured data is written — a build that cannot name its own origin is not indexable, by design |
| 2   | `X-Robots-Tag: noindex` on the disallowed paths                                                | Host / CDN | Silent. `Disallow` stops crawling, not listing; a disallowed URL can still appear as a bare link. Only a response header closes it, and this bundle sends no headers                          |
| 3   | A real HTTP `404` for unknown paths                                                            | Host / CDN | Silent. History fallback answers `200` with the shell for every path, so an unknown URL is a soft 404. The user-facing page is correct and translated; the status line is not                 |

None of the three is new. All three were recorded in A64-026.3 and are
re-confirmed here — the audit found no deployment configuration in this
repository that could implement 2 or 3, so they stay with the host.

### Deferred, with a reason rather than a hope

| Item                               | Class                        | Why it waits                                                                                                                            |
| ---------------------------------- | ---------------------------- | --------------------------------------------------------------------------------------------------------------------------------------- |
| Crawlable page body / prerendering | P1 post-beta                 | Needs a hydration path. `createRoot().render()` would discard a prerendered DOM; `docs/04-frontend/seo.md` §6 records this as ADR-sized |
| Tournament indexing                | P1 post-beta                 | Needs per-route metadata, which needs the same hydration path (§5)                                                                      |
| Player-profile indexing            | Intentional product decision | Requires a new, explicitly opt-in preference defaulting to off. An opt-in can be added later; an index cannot be un-indexed             |
| Public game replays                | Intentional product decision | Needs a visibility rule `specs/game` does not have (§4)                                                                                 |
| Leaderboard surface                | Out of scope                 | Product work: the API has a ladder, the application has no page (§6)                                                                    |
