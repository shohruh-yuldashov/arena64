# Friend Challenges

| | |
| --- | --- |
| **Status** | Domain and persistence — A64-022.1. No API, no UI, no realtime, no notification |
| **Owner** | platform |
| **Last updated** | 2026-08-07 — A64-022.1, domain and persistence |
| **Related** | `docs/01-architecture/domain-model.md` §10.3, `specs/matchmaking.md`, `specs/friends.md`, `specs/notifications.md` §15.15 |

A **friend challenge** is a direct, named invitation: one player asking one
friend to play one game, at settings the challenger chose. It survives both
players signing out, and it resolves when the recipient answers.

## 1. Scope of A64-022.1

Built: the aggregate, its invariants, its persistence, and the application
commands that prove them.

**Not** built, and each is a later phase rather than an omission:

| | Phase |
| --- | --- |
| HTTP API | A64-022.2 |
| Acceptance and match creation | A64-022.3 |
| Realtime frame and notification | A64-022.2 |
| Frontend | A64-022.4 |
| Expiry sweep | A64-022.6 |

## 2. Where it lives, and why

`matchmaking`. Not a module of its own, and this is `domain-model.md`'s
placement rather than a preference — §10.3 lists `Challenge` beside
`QueueTicket` in this context, and its own comparison table says why:

| | `QueueTicket` | `Challenge` |
| --- | --- | --- |
| Opponent | chosen by rating | named at creation |
| Lifetime | seconds to minutes | hours to days |
| Resolution | the pairing worker | the recipient |

Both are *intentions to play* that resolve into a `Match`. They differ in who
picks the opponent and how long the intention lives, not in what they are.

> `specs/notifications.md` §15.15 previously named "a new `challenges`
> module" as the seam. That was written before this audit and is wrong;
> the seam is otherwise unchanged.

## 3. Lifecycle

```
                 ┌─→ ACCEPTED   (A64-022.3 — creates the match)
                 │
    PENDING ─────┼─→ DECLINED   (the recipient)
                 ├─→ CANCELLED  (the challenger)
                 └─→ EXPIRED    (the platform, after 24h)
```

Every non-`PENDING` state is terminal. There is no reopening and no editing:
a challenger who wants different settings cancels and sends another.

`ACCEPTED` is **unreachable in A64-022.1**. `domain-model.md` §10.3 requires
acceptance to create the match in the same transaction that consumes the
challenge, so a transition that moved the status and created no match would
be a challenge claiming a game nobody can play. It arrives with match
creation, in one place, in A64-022.3.

`Voided-by-block` — §10.3's sixth state — is **deferred**. A block placed
after a challenge is sent should void it, which needs a consumer of
`friends.player_blocked`; that is a phase with an event subscriber in it. In
the meantime a block cannot be *bypassed*: acceptance re-checks it.

## 4. Who may challenge whom

Server-owned, every part of it, from the modules that own the answers.

| Rule | Authority | On failure |
| --- | --- | --- |
| Different players | the aggregate — needs nothing but the two ids | `ChallengeSelfNotAllowed` |
| The time control is offered | `reference.TimeControlCatalogue.active` | `ChallengeInvalidTimeControl` |
| They are friends | `friends.SocialGraphReader.friend_ids_among` | `ChallengeNotFriends` |
| Neither has blocked the other | `friends.PairingExclusions.blocked_pairs_among` | `ChallengeNotFriends` |
| No live challenge between them | `uq_friend_challenge__live_pair` | `ConflictError` |

Checked in that order: cheapest and most final first, so a self-challenge or
an unknown clock is refused before the social graph is read at all.

### Blocking and non-friendship are the same answer

`domain-model.md` §10.3, BL-2 and FR-2: *"a challenge to a blocked player
fails indistinguishably"*. There is deliberately **no `challenge_blocked`
error code** — one that existed would be the disclosure, whatever message sat
beside it. Both cases raise `ChallengeNotFriends` with the same sentence, and
a test asserts the strings are equal.

## 5. Expiry — 24 hours

`domain-model.md` §10.3 gives the shape ("hours to days, survives sign-out")
and no number. Twenty-four hours is this phase's product decision.

A challenge sent on a Monday evening is seen by Tuesday evening whatever
hours the two players keep, and a day is short enough that an unanswered
invitation stops being a live commitment before either has forgotten sending
it. It is also `auth`'s verification window, so the platform has one "a day".

**Server-authoritative, and enforced twice:**

| Where | What it does |
| --- | --- |
| `Challenge.is_expired_at` | refuses an answer past the window, on the read path |
| the sweep (A64-022.6) | writes the terminal `EXPIRED` row and emits the event |

Both are needed. Without the read-time check a challenge could be answered
between expiry and the sweep; without the sweep, `EXPIRED` would be a state
the database never holds and every reader would re-derive it — and no
consumer could react to it, so a stale invitation would sit on a screen.

Cancelling an **expired** challenge is permitted, unlike answering one. That
is somebody clearing a list, and refusing it would leave a row they cannot
remove until a sweep they cannot see runs.

## 6. One live challenge per pair

Policy **A**: one live challenge per *unordered* pair — not one per
direction.

    A→B pending, then B→A   refused
    A→B pending, then A→B   refused
    A→B declined, then B→A  allowed

Enforced by `uq_friend_challenge__live_pair`, a partial unique index keyed on
`least(challenger_id, recipient_id), greatest(...)`. Two reasons it is an
index rather than a service check:

**Unordered.** A plain unique on the two columns permits exactly the
opposite-direction case the rule exists to prevent — two friends challenging
each other simultaneously, ending with two games.

**Structural.** A check-then-insert loses the race between two simultaneous
creates, and the pair that races is precisely the pair the rule is about.
`find_live_between` exists only so the service can produce a readable message.

Partial on `pending`, so the rule is about the *live* state: a plain unique
would mean two friends could challenge each other once ever.

## 7. Match settings snapshot

The challenge stores what a match needs and nothing else, so A64-022.3 can
create one without asking the client again.

| Stored | Why |
| --- | --- |
| `time_control_id` | a `TimeControlId`, the stable code `reference` publishes |
| `variant` | `CreateMatchRequest` requires it; a challenge that did not record which game it was for becomes ambiguous retroactively the day a second variant ships |
| `rated` | what the challenger **asked** for — see §8 |

Not stored: usernames, display names, avatars, rating snapshots, or a
human-readable time control label. Those belong to `profiles`, `rating` and
`reference`, and they change — a challenge that copied one would show
yesterday's name on today's screen and would be a second place a private
profile could leak from.

`time_control_id` is validated against the **active** catalogue, not the
enum, so a control retired since stays readable on old rows and cannot be
chosen for a new challenge.

## 8. Rated — both players must agree

Arena64's decision for this epic: a direct game between friends affects
ratings only when **both** players have agreed to it.

The reason is that two friends who can rate a game between themselves can
move rating between themselves — one loses on purpose, repeatedly. Queue
matches have no such exposure because neither player chooses the opponent.

The aggregate stores one half. `rated` is the challenger's **request**; the
recipient's consent is a separate act at acceptance, which lives in
A64-022.3 along with acceptance itself.

**Nothing in A64-022.1 can produce a rated match, because nothing in
A64-022.1 can produce a match.** The field defaults to `False` and the
column exists so the schema does not change when acceptance arrives.

## 9. Domain events

Four, in `matchmaking.domain.challenge_events`, `aggregate_type =
"challenge"`:

    matchmaking.friend_challenge_created
    matchmaking.friend_challenge_declined
    matchmaking.friend_challenge_cancelled
    matchmaking.friend_challenge_expired

Every payload carries the challenge id and **both** player ids, because a
consumer's first act is deciding whom to tell and re-deriving the other party
would mean reading a row a sweep may have removed. `created` additionally
carries the settings and `expires_at` — a notification that said only
"somebody challenged you" would make the recipient open the app to learn what
they were asked to play.

No prose, no usernames, no ratings. A consumer that wants to say "Aziz
challenged you" composes that through `profiles`, which owns names and knows
whether the viewer may see one.

**Nothing publishes them yet.** They exist because their consumers —
A64-022.2's realtime frame and notification, A64-022.3's match creation — are
the next phases, and a producer added after its consumers has no record of
what happened before it. Wiring the publisher is one constructor argument and
belongs with the first consumer.

`FriendChallengeAccepted` is deliberately absent: it announces a transition
this build cannot make.

## 10. Invariants

Enforced in the aggregate, which is framework-free:

- only `PENDING` may transition; the other four are terminal
- the **recipient** declines; the **challenger** cancels; neither may do the other's
- an expired challenge cannot be declined
- a terminal challenge has a `responded_at`; a pending one does not
- every timestamp comes from the injected clock (AD-07), never `datetime.now()`

Three of these are also `CHECK` constraints, for the second writer — a future
backfill, an operator's `UPDATE`, a repository written next year — rather
than because the aggregate is untrusted.

## 11. Persistence

`matchmaking.friend_challenge`, one row per challenge.

**No foreign keys.** `challenger_id` and `recipient_id` are opaque
cross-context identifiers (DM-06), so the schemas stay deployable apart.
`created_match_id` has none either, and that one is retention: a challenge is
the record of an invitation and must outlive the game it produced.

| Index | Serves |
| --- | --- |
| `uq_friend_challenge__live_pair` | the one-live-pair rule (unique, partial) |
| `ix_friend_challenge__recipient_pending` | "who has invited me" (partial) |
| `ix_friend_challenge__challenger_pending` | "what have I sent" (partial) |
| `ix_friend_challenge__expiring` | the sweep's claim query (partial) |

All partial on `pending`, so a table that accumulates answered history costs
nothing to scan. Two indexes rather than one composite for the two screens,
because they put different players in the predicate.

Migration `1ba6f5d18023`. No backfill — nothing existing is a challenge.

## 12. Concurrency

| Race | Resolved by |
| --- | --- |
| two creates, same direction | `uq_friend_challenge__live_pair` |
| two creates, opposite directions | the same index — it is keyed on the unordered pair |
| decline vs cancel | `save` guards on `status = 'pending'`; the second `UPDATE` matches no row |
| answer vs expire | the same guard, plus the read-time expiry check |
| two declines | the same guard |

The sequential case is caught earlier, by the re-read inside the unit of
work: the second actor's aggregate is already terminal and the *domain*
refuses it. The database guard is for the genuine race, where neither actor
has a re-read to catch it and neither is wrong.

## 13. Error taxonomy

| Error | Meaning |
| --- | --- |
| `ChallengeSelfNotAllowed` | the recipient is the challenger |
| `ChallengeNotFriends` | not friends **or** blocked — indistinguishable by design |
| `ChallengeInvalidTimeControl` | the clock is not currently offered |
| `ChallengeNotPending` | already answered — one error for all four terminal states, so a challenger is not told *which* |
| `ChallengeExpired` | still pending, past the window |
| `ChallengeForbidden` | a party to the challenge, using the wrong verb |
| `NotFoundError` | no such challenge, **or** one between two other people |
| `ConflictError` | a live challenge already exists, or the row was settled first |

No SQL constraint name reaches a caller: `add` translates the integrity error
inside the repository.

## 14. Security

| Guarantee | How |
| --- | --- |
| The actor is server-owned | `challenger_id` and `by` come from the session; no parameter a transport could fill from a body |
| Friendship cannot be asserted by a client | asked of `friends`' own reader, every time |
| A block cannot be bypassed | the symmetric `blocked_pairs_among`, checked at creation and again at acceptance |
| A challenge id cannot be probed | every read is scoped to a party; a stranger gets `NotFoundError`, never `Forbidden` |
| Settings cannot be changed after sending | `save` writes three columns; the parties and settings are not among them |
| The match link cannot be forged | `created_match_id` is written by the platform in the transaction that creates the match, and a `CHECK` refuses it on any non-accepted row |
| No private profile data is stored | the aggregate holds two opaque ids and three settings |
| Logs carry no social graph | ids and a boolean; no names, no settings |

## 15. Performance

| Operation | Statements |
| --- | --- |
| create | 1 catalogue read + 1 friendship read + 1 block read + 1 insert |
| decline / cancel / expire | 1 scoped read + 1 guarded update |
| duplicate create | the same, ending in a refused insert — no extra read |
| live-pair lookup | 1, on the unique index's own expressions |

No N+1: the two social reads are batch ports called with one element each,
so the singular use case costs the same query with a one-element `IN`. No
cache — challenge state is durable product state, and `SocialGraphCache` is
`friends`' own decorator rather than a cache of challenges.

## 16. Deferred

| | Notes |
| --- | --- |
| `ACCEPTED` | A64-022.3, with match creation, in one transaction |
| `Voided-by-block` | needs a `friends.player_blocked` consumer |
| Open (link-shareable) challenges | §10.3 allows them; this epic is directed-only |
| Rematch | explicitly out of scope |
| Tournament challenge semantics | out of scope |
| Event publication | with the first consumer |
| Expiry sweep | A64-022.6 |
