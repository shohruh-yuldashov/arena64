"""`reference` — the platform's shared reference data. database.md DB-08.

DB-08 puts variants, time controls, regions and locales in a `reference`
schema, and until A64-020.5A-pre nothing implemented it. Five files across
`matchmaking`, `game` and `rating` recorded the gap in the same words —
"`reference.time_control` is specified and does not exist in code" — and each
declined to invent a local substitute. This module is what they were waiting
for.

## One table today, and that is the whole module

`reference.time_control`. Nothing else, because nothing else has a consumer:
`ProductVariant` and `Region` are still enums in the modules that own them,
and moving them here would be a migration bought with no requirement
(CLAUDE.md §1.7).

Their docstrings already say what happens when that changes — `Region`
predicts "this becomes `reference.region` and the column a foreign key" —
so this module's shape is set by the first entry rather than by a plan for
five.

## Why a bounded context rather than `app/platform`

`app.platform` may import no module (`platform-imports-no-module`), and a
time control carries a `rating.public.SpeedClass`. So reference data that
speaks any module's vocabulary cannot live in `platform`, and this is a
module like any other: reachable only through `reference.public`, its
internals held private by `reference-internals-are-private`.

## What reference data is, and what it is not

It is a **closed, platform-owned catalogue**: small, slow-moving, and
authored by the platform rather than by a player. It is not configuration —
a tier that offered a different set of time controls would produce ratings
incomparable with every other tier's, which is the argument
`DEFAULT_SPEED_CLASS` and `STARTING_RATING` both record.
"""
