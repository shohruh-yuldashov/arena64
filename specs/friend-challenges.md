# Friend Challenges

| | |
| --- | --- |
| **Status** | Complete through acceptance — A64-022.3. No UI, no realtime, no notification |
| **Owner** | platform |
| **Last updated** | 2026-08-07 — A64-022.3, acceptance and match creation |
| **Related** | `docs/01-architecture/domain-model.md` §10.3, `specs/matchmaking.md`, `specs/friends.md`, `specs/notifications.md` §15.15 |

A **friend challenge** is a direct, named invitation: one player asking one
friend to play one game, at settings the challenger chose. It survives both
players signing out, and it resolves when the recipient answers.

## 1. Scope of A64-022.1

Built through A64-022.2: the aggregate, its invariants, its persistence, the
application commands, the authenticated HTTP API, and the lifecycle events
three later phases consume.

**Not** built, and each is a later phase rather than an omission:

| | Phase |
| --- | --- |
| Realtime frame and notification | A64-022.4 |
| Frontend | A64-022.5 |
| Expiry sweep | A64-022.6 |
| Terminal history endpoint | undecided — see §17 |

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

## 16. HTTP API — A64-022.2

| Route | Auth | Limit |
| --- | --- | --- |
| `POST /challenges` | `VerifiedUser` | `challenge_create_user`, 20/hour |
| `GET /challenges/incoming` | `CurrentUser` | none |
| `GET /challenges/outgoing` | `CurrentUser` | none |
| `GET /challenges/{id}` | `CurrentUser` | none |
| `POST /challenges/{id}/accept` | `VerifiedUser` | `challenge_respond_user`, 60/5min |
| `POST /challenges/{id}/decline` | `VerifiedUser` | the same bucket |
| `DELETE /challenges/{id}` | `VerifiedUser` | the same bucket |

`POST /challenges/{id}/accept` is §18.

### 16.1 Create

Accepts exactly four fields — `recipient_id`, `time_control_id`, `variant`,
`rated` — with `extra="forbid"`, so a body carrying `challenger_id`,
`status`, `expires_at` or `created_match_id` is a `422` rather than a
silently ignored field. The actor is the session's, always.

### 16.2 Reading, and who may

Every route is scoped to a party. A challenge between two other people is
**`404`**, never `403`: an identifier that answered differently would be an
existence oracle for a UUID somebody could otherwise probe.

`403` is reserved for the two people who *are* parties and used the wrong
verb — a challenger declining, a recipient cancelling. Both already know it
exists, so hiding it would be a fiction rather than a protection.

### 16.3 Lists are live-only

Both lists return **pending, unexpired, still-permitted** challenges, newest
first, keyset-paginated. A terminal challenge leaves them silently; the row
is not deleted, and it stays readable by id — a client holding an identifier
deserves to learn the invitation was declined rather than that it vanished.

**There is deliberately no history endpoint.** Whether a player wants a log
of past invitations is a product decision nobody has taken, and an unbounded
one added quietly would be a list nobody designed.

Expiry is applied **in the query**, so `limit` means what it says for that
predicate. The relationship filter is applied **after** the page — see below.

### 16.4 Unfriending and blocking after creation

A live challenge disappears from both lists the moment the two stop being
friends. Removing the friendship is the general test and covers blocking too,
because a block also ends the friendship — which is why this module needs no
notion of blocking and BL-2 is satisfied without one.

This is a **visibility rule, not the security boundary**. The row is still
stored and still readable by id, and acceptance re-checks the relationship in
A64-022.3, so a stale invitation cannot become a game even if something
failed to hide it. Persistence cleanup stays deferred to A64-022.6, and no
new terminal state was invented for it.

The filter runs in the application layer rather than the query because the
friendship lives in another module's schema, and a join would be the
cross-context reach DM-06 designs against. The consequence is stated on the
endpoints: `limit` is an upper bound on a page, not a promise.

### 16.5 Response

The challenge's facts plus the **other party's** public profile, composed
through `profiles`' batch directory — so it obeys exactly the privacy rules
`GET /profiles/{username}` does, and this module re-derives none of them.

One batch lookup per page. A page of twenty costs one challenge query and one
profile batch, never twenty-one queries.

A challenge whose counterpart has been deactivated is **omitted** from a list
and **500s** on the singular read. The asymmetry is deliberate: a client that
asked for one specific challenge deserves to know something is wrong, where a
list that failed entirely because one row's counterpart withdrew would be a
screen nobody can use.

### 16.6 Errors

| Code | HTTP | |
| --- | --- | --- |
| `challenge_self_not_allowed` | 422 | you named yourself |
| `challenge_not_friends` | 422 | not friends **or** blocked — indistinguishable |
| `challenge_invalid_time_control` | 422 | that clock is not offered |
| `challenge_already_pending` | 409 | one is already live between you |
| `challenge_not_pending` | 422 | already answered |
| `challenge_expired` | 422 | too late |
| `permission_denied` | 403 | a party, wrong verb |
| `not_found` | 404 | no such challenge **of yours** |

No SQL constraint name reaches a caller, and there is deliberately no
`challenge_blocked` — see §4.

### 16.7 Events

Three of the four are published, each **inside the transaction that made the
fact true** (AD-16), so a challenge that committed without its event cannot
exist:

    create   → matchmaking.friend_challenge_created
    decline  → matchmaking.friend_challenge_declined
    cancel   → matchmaking.friend_challenge_cancelled

Only after a transition that actually happened: `save` raises on a row
somebody else settled first, so a losing writer never reaches the publish and
a duplicate decline emits no second event.

`friend_challenge_expired` is published by the sweep that writes the terminal
row (A64-022.6), not by `ChallengeService.expire` — emitting from both would
announce one challenge twice.

`occurred_at` is the aggregate's own timestamp, never a second clock read.

### 16.8 Downstream seams

**A64-022.4 notifications.** `friend_challenge_created` carries the challenge
id, both player ids, the settings and `expires_at` — everything a
`friend_challenge_received` notification needs, with no prose, no names and
no channel-specific payload. A consumer that wants to say "Aziz challenged
you" composes it through `profiles`, which owns names and knows whether the
viewer may see one.

**A64-022.4 realtime.** No gateway frame and no socket call from
`matchmaking`. The relay already carries these events, and a consumer built
beside `SocialNotificationDispatcher` reaches the existing gateway.

**A64-022.3 match creation.** The seam is unchanged and now smaller: add
`accept` to the aggregate and the service, create the match in the same
transaction, write `created_match_id`, publish two events. No new table, no
schema change — the column and the `CHECK` are already there.

## 18. Acceptance — A64-022.3

The recipient says yes, and a game exists by the time the request returns.

`POST /challenges/{id}/accept`, `VerifiedUser`, **no body**. The recipient
accepts exactly the proposal already stored; a settings field would be a way
to change what was agreed after agreeing to it.

### 18.1 One transaction, and how two services share it

Four things must land together or not at all: the match, the challenge's
transition, `game.match_created` and
`matchmaking.friend_challenge_accepted`.

`MatchCreationUseCase` commits by contract — correct for every caller that
only creates a match, wrong for this one. Rather than special-casing that
inside `game`, acceptance hands it a **`ParticipatingUnitOfWork`**: a unit of
work that stages and flushes and leaves the commit to its caller. `game` is
unchanged, its other callers are unchanged, and the caller that needs to own
the transaction says so by construction.

    1. load, scoped to a party
    2. re-check the relationship
    3. resolve the clock
    4. read both rating snapshots
    5. create the match          ← stages, flushes, does not commit
    6. challenge.accept(match_id)
    7. save, guarded on pending
    8. stage the accepted event
    9. commit                    ← all four, once

The match is created **first** because its identity is generated at
persistence and the challenge has to record it. The window that ordering
would normally open — a match with no accepted challenge — does not exist,
because nothing has committed until step 9.

### 18.2 What is revalidated, and why the snapshot is not enough

| | Why |
| --- | --- |
| Friendship and blocks | Mutable. Twenty-four hours is long enough for a friendship to end, and the creation-time check is not authority for state that changes |
| Expiry | Server-authoritative; a device's clock has no say |
| The time control | `reference.require` refuses a retired one, and **no reader returns an inactive control's parameters** — so a clock withdrawn after the invitation was sent makes it unacceptable. That is the seams' only possible behaviour rather than a rule chosen here |

Any of them failing means no match, no acceptance, and a challenge left
`pending`.

### 18.3 Rated consent

The challenger chose `rated` when sending. **Accepting is the consent**, and
`Match.rated` is always the challenge's — there is no second flag anywhere,
and the accept request has no body to put one in.

### 18.4 Seats

`Pairing.of`'s policy, reused: the **parity of the derived pairing id**
decides who plays light. Its own docstring rejects "whoever waited" and
"lower rating" because both hand a measurable edge to a predictable player,
in rated games, forever — and "the challenger always moves first" is exactly
that shape, so it is not what happens.

### 18.5 Why the match waits to be joined

`MatchRecord` refuses a **system-activated match that carries a time
control**: the first flag deadline is written when a match activates, and the
one place that happens is `MatchAcceptanceService`. A system-activated timed
match would start a clock nothing had scheduled a deadline for — a game that
can never flag. The invariant says so in as many words, and says it exists so
a later task is *made* to schedule the deadline.

A challenge match is timed, so it is created `BILATERAL` and activated by the
existing handshake, which does schedule it. That is also what
`domain-model.md` §10.3 describes: *"resolves through the `Created` join
deadline"* — a match that resolves through a join deadline is one that was
waiting to be joined. The join window is ten minutes.

For a player that is one tap on a screen they are already looking at, and
A64-022.5 can make it a single action; the protocol is the one that already
works.

### 18.6 Match origin, and one challenge → one match

`origin = CHALLENGE`, `origin_ref = challenge_id`, both server-owned —
nothing from the request reaches them, so a client cannot claim a match came
from a challenge it did not.

`pairing_id` is a `uuid5` derived from the challenge id, and `game` enforces
`uq_match__pairing_id`. So a second acceptance cannot produce a second match
**even if the guarded challenge update let it through** — two independent
defences, and no new constraint was needed for either.

### 18.7 Races

| Race | Outcome |
| --- | --- |
| Two accepts | One match, one `ACCEPTED`, one event. The second is `challenge_not_pending` |
| Accept vs cancel | Whichever commits first wins. `CANCELLED + Match` cannot happen: the guarded update refuses the loser before any match is created |
| Accept vs expiry | The window is checked inside the transaction, against the injected clock. An accepted challenge is terminal, so a later sweep cannot expire it |
| Accept vs unfriend/block | Checked inside the transaction, as close to creation as the architecture allows. If the relationship change commits first there is no match; if acceptance commits first the match exists and a later change does not retroactively remove it — a game that was legitimately started is a game that was played |

### 18.8 Events

`matchmaking.friend_challenge_accepted` carries the challenge id, both
players, **the match id** and the settings. No prose, no names, no URLs.

`game.match_created` is `game`'s own and is not duplicated here. Both are
staged in one transaction, so a consumer sees both or neither; within a relay
pass `match.created` is stamped first. Nothing depends on that ordering
today, and it is written down so a consumer that does depend on it finds it
rather than discovers it.

### 18.9 Matchmaking interaction

**Nothing is checked**, deliberately. This platform has no "one active match"
rule: the queue does not check for an active match, tournaments do not, and
`QueueEligibilityPolicy` governs entering a *pool* rather than starting a
game. Adding one to challenges alone would make them stricter than every
other path into a match, and whether the platform wants such a rule is a
decision that belongs to all three at once.

### 18.10 The handoff

`created_match_id` on the response is the game. There is deliberately no URL
in the payload: a route is the client's to build, and a server-supplied one
is a redirect nobody validated.

### 18.11 Notification and realtime seams — A64-022.4

`friend_challenge_accepted` carries everything a `friend_challenge_accepted`
notification needs, including the match id so the notification can offer the
game. No gateway frame and no socket call from `matchmaking`; a consumer
built beside `SocialNotificationDispatcher` reaches the existing gateway.

Challenge matches are not excluded by any origin filter — `MatchOrigin` is
stored and handed back, never branched on.

## 19. Deferred

| | Notes |
| --- | --- |
| `Voided-by-block` | needs a `friends.player_blocked` consumer |
| Open (link-shareable) challenges | §10.3 allows them; this epic is directed-only |
| Rematch | explicitly out of scope |
| Tournament challenge semantics | out of scope |
| Event publication | with the first consumer |
| Expiry sweep | A64-022.6 |
