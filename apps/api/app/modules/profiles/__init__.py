"""The `profiles` bounded context — a player as everyone else sees them.

## Why this is its own module and not a route on `users`

A public profile is a **composition**, not a record. The fields on it are
owned by three different contexts and will be owned by more:

    identity, join date, country, bio    `users` (domain-model.md §7)
    ratings per category                 `rating` — not yet built
    games played, wins, losses, draws    `statistics` — not yet built

Putting the endpoint on `users` would make `users` import `rating` and
`statistics` to render it, which inverts the dependency: those two are
downstream of identity, not upstream. Putting it here means `profiles`
depends on all three and none of them depends on it — the shape
architecture.md AD-08 prescribes for cross-context reads ("read models,
not repositories").

The practical test: when `rating` ships, nothing in `users` changes. The
port `profiles` already programs against gains a real implementation and
the placeholder is deleted.

## What this module owns

Exactly one decision: *what a stranger may see about a player, and how it
is composed*. It owns no table, no migration and no write path. Every
value it renders is read through a published port belonging to somebody
else, which is why `infrastructure/` here holds providers rather than
repositories.

## What is deliberately not here

Avatar upload, friends, followers, game history, online status, privacy
controls and profile editing are all excluded by A64-012.1's brief. None
is stubbed. The two nulls the response does carry — `last_seen`, and a
`bio`/`country` that no endpoint writes yet — are documented at the
schema, because a response field with no source behind it is exactly the
thing a later reader must not mistake for working.

## Layout

Mirrors `users` and `auth` (`domain` / `application` / `infrastructure` /
`presentation`), for the reason services.md §2.1 gives: uniformity is
worth more than local optimisation, because a contributor who has read one
module can navigate the next. There is no `public/` — nothing consumes
`profiles` yet, and BE-03's surface is published when a consumer exists,
not before.
"""
