# `entities/`

Business nouns and the shapes that describe them — a player, a tournament,
a match — together with the query hooks and mappers that belong to the noun
rather than to any one screen.

**Deliberately empty at A64-020.1.** This phase builds infrastructure only,
and there is no business UI to have entities for. An entity invented before
a screen needs it is CLAUDE.md §1.7's speculative generality: it would be
shaped by a guess about the first consumer rather than by the consumer.

The layer exists now because `eslint.config.mjs` enforces the dependency
direction across all six layers, and a layer that appears later tends to
appear _around_ the imports that already went the wrong way.

## What belongs here

| Belongs                                                    | Does not                                                       |
| ---------------------------------------------------------- | -------------------------------------------------------------- |
| `entities/player/model.ts` — the shape and its type guards | A page or a route                                              |
| `entities/player/api.ts` — `getPlayer()` over `shared/api` | A form, a dialog, a layout                                     |
| `entities/tournament/lib/format-rank.ts`                   | Anything importing `features/`, `widgets/`, `pages/` or `app/` |

May import: `shared/`. Nothing else.
