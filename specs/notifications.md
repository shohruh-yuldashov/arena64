# Notifications

> **Status:** **production-ready** — audited A64-021.7. Foundation A64-021.1; realtime A64-021.2; preferences A64-021.3; event coverage A64-021.4; email through Resend A64-021.5; Web Push A64-021.6; social push A64-021.6A
> **Owner:** platform
> **Related:** `docs/01-architecture/domain-model.md` §9.3, `docs/01-architecture/database.md` §10.2 and §10.3, `specs/friends.md`, `specs/frontend.md` §21 and §22

A **notification** is a durable, recipient-owned record that something
happened which a player should know about — NT-1: *"the notification exists
even if every delivery channel fails."*

A64-013.7 built the pipeline that decides *who may be told what* — the
outbox, the relay, the audience resolution, the privacy gate — and stopped
at a log line, because there was no channel to deliver into. A64-021.1 adds
the thing NT-1 is actually about: the record, and the screen that reads it.

A64-021.2 adds the **first delivery channel**: the recipient is told over
the socket they already have, the moment the row is committed (§11). Push
and email remain deferred, and §12 states exactly what each must add.

A64-021.3 adds the **preferences** that govern all of it (§10). A muted
category produces no durable row and no realtime frame — suppression at the
point of creation, never a filter applied on read.

A64-021.5 adds the **second delivery channel**: transactional email for the
three tournament types, opt-in, to verified addresses only, through a
durable queue that survives a restart (§13). It delivers through **Resend**
from `no-reply@arena64.gg`, and a process without the credential reports the
channel unavailable rather than pretending otherwise.

A64-021.4 extends coverage from two types to **six**: three tournament facts
and one game result, each from an event that already existed or — in one
case — one added additively to a transaction that already committed the fact
(§2). Nothing about the transport, the preferences or the exactly-once
guarantee changed to accommodate them.

---

## 1. What a notification is, and is not

| It is | It is not |
| --- | --- |
| A projection of a source event, owned by one recipient | A second copy of the source aggregate |
| A record of what was true when it was written | A live view that re-resolves on read |
| Deletable in bulk without losing truth | The system of record for anything |

Every row is derived from a `platform.outbox` event that is itself durable,
so the friendship, the request and the match are all still there if the
whole relation is dropped.

**The actor's name is a snapshot.** A two-week-old notification shows the
display name its actor had two weeks ago. Re-resolving at read time would
cost one profile lookup per row — the N+1 §8.4 forbids — and would rewrite
history every time somebody renamed themselves.

---

## 2. Supported types

Eight, and each has a source event that names its recipients unambiguously.

| Type | Category | Source event | Recipients | Target | Phase |
| --- | --- | --- | --- | --- | --- |
| `friend_request_received` | `social` | `friends.friend_request_sent` | the addressee | `friend_requests` | A64-021.1 |
| `friend_request_accepted` | `social` | `friends.friend_request_accepted` | the requester | `player_profile` | A64-021.1 |
| `tournament_registration_confirmed` | `tournament` | `tournament.player_registered` | the entrant | `tournament` | A64-021.4 |
| `tournament_round_published` | `tournament` | `tournament.round_published` | every **live** entrant | `tournament` | A64-021.4 |
| `tournament_completed` | `tournament` | `tournament.completed` | everybody with a **standing** | `tournament` | A64-021.4 |
| `game_completed` | `game` | `game.match_completed` | both seats | `match_replay` | A64-021.4 |
| `friend_challenge_received` | `social` | `matchmaking.friend_challenge_created` | the recipient | `challenges` | A64-022.4, retargeted in A64-022.5 |
| `friend_challenge_accepted` | `social` | `matchmaking.friend_challenge_accepted` | the **challenger** | `live_game` | A64-022.4 |

**No actor is told what they just did.** Sending a request notifies the
addressee; accepting one notifies the requester. A notification about your
own action is not a notification — the exception is
`tournament_registration_confirmed`, and it is deliberate: entering a
tournament is a commitment to turn up at a time the player does not choose,
and the receipt is the thing they look back for.

`friends.friend_request_sent` is new in A64-021.1 and
`tournament.player_registered` in A64-021.4. In both cases the fact existed
already and reached only a log line. Publishing it changes nothing about the
request or the registration itself.

### 2.1 Categories

`social`, `game`, `tournament`, `system`. Three have producers as of
A64-021.4; `system` does not, and is the only one a player may never mute
(§10.1).

`marketing` is deliberately absent: this product defines no such
notification, and a category nothing produces is a preference that silently
does nothing.

### 2.2 Recipient rules, stated exactly

| Type | Rule | Where it is enforced |
| --- | --- | --- |
| `tournament_registration_confirmed` | The one player named on the event | Nothing is read back — the event carries the player and the tournament's name |
| `tournament_round_published` | `status = REGISTERED` at **delivery** time | The audience query's own predicate, so a withdrawal between publication and delivery excludes them without a filter anybody has to remember |
| `tournament_completed` | Anyone with a row in `standing` | A player who withdrew before the field was fixed has no result, and telling them where they did not place is worse than silence |
| `game_completed` | The two seats on the event | Nothing is read back |
| `friend_challenge_received` | The recipient named on the event, **unless the pair is blocked at delivery time** | `SocialGraphReader.blocked_ids_for`, re-read by the consumer — never the enqueue-time state |
| `friend_challenge_accepted` | The challenger named on the event | Nothing is read back, and no block suppresses it — see `specs/friend-challenges.md` §21 |

**No recipient ever comes from a client.** Every one is derived from the
event or from a `tournament.public` read; there is no endpoint, parameter or
payload field through which a caller can name who is notified.

### 2.3 Deferred types, and the seam each is missing

| Candidate | Why not yet |
| --- | --- |
| `tournament_match_ready` | **No source event.** Matches are created by `TournamentMatchLauncher`, which holds no publisher, and `game.match_activated` carries no `origin` — so a consumer cannot tell a tournament fixture from a queue pairing. Adding one also needs the launcher to report whether the attempt was *newly* recorded, or a re-launch after a restart would produce a second notification. Three deliberate changes, not one |
| `tournament_cancelled` | The event is declared and **never published**: no application service emits it. A consumer for it would be an entry point nothing reaches |
| `rating_changed` | `rating.updated` exists, but neither the product value nor the safe payload is settled — a rating is a number a player sees on their own profile, and a row per game saying it moved would be a second copy of `game_completed` |
| `match_found` | `game.match_created` has every fact needed, and the offer expires in seconds. A durable row for it would be a list full of dead offers; it belongs with realtime delivery |
| `friend_online` / `friend_offline` | Genuinely transient. A row per transition would be thousands a day in a list whose value is that it is short |
| every move, draw offers, typing | Live game state and short-lived commands, not history |
| `friend_challenge_declined` | The challenger learns nothing actionable — a decline carries no reason by design, so the row would say "no", permanently, in a list whose value is that it is short |
| `friend_challenge_cancelled` | A **retraction**. Its consumer is a surface that must stop showing an invitation, not an inbox that must start showing one |
| `friend_challenge_expired` | Nothing happened. A row saying so is an inbox entry about the absence of an event |

The distinction those last four draw is the one that matters: **not every
notification is durable.** An event whose fact is wrong by the time it is
read should never become a permanent record.

---

## 3. Payload

Each type has a typed payload, decoded against the row's own `type` on the
way out. A row whose JSON does not match raises rather than reaching a
client half-rendered.

Four shapes, and `type` decides which:

**`ActorSummary`** — the two social types.

| Field | Note |
| --- | --- |
| `actor_player_id` | |
| `actor_username` | |
| `actor_display_name` | Nullable |
| `actor_avatar_object_key`, `actor_avatar_version` | **Not a URL** — the URL is composed at the presentation boundary by `AvatarLinkBuilder`, so no CDN hostname is frozen into a historical row |

**`TournamentSummary`** — the three tournament types.

| Field | Note |
| --- | --- |
| `tournament_id` | |
| `tournament_name` | A **snapshot**. A renamed tournament does not rewrite a receipt somebody already has |
| `round_number` | `null` except for `tournament_round_published` |
| `final_rank` | **This recipient's** placement, never the winner's. `null` when they have no standing. Ranks are as recorded — ties share one, gaps are real |

**`GameResultSummary`** — `game_completed`.

| Field | Note |
| --- | --- |
| `match_id` | |
| `outcome` | `win`, `loss` or `draw`, already resolved **from the recipient's point of view** — a client renders "you won" without knowing which seat it held |
| `termination_reason` | `game`'s own value. "You lost" and "you lost on time" are different sentences, and an adjudicated result is the case this type exists for |
| `opponent` | An actor summary, or `null` when that account no longer has a profile. The game was still played |

**`ChallengeSummary`** — the two challenge types, A64-022.4.

| Field | Note |
| --- | --- |
| `challenge_id` | |
| `opponent` | An actor summary — the challenger on a received invitation, the recipient on an accepted one. `null` when that account no longer has a profile |
| `time_control_id` | The catalogue **code**, never raw clock numbers. A client renders it through the same catalogue every other surface reads |
| `variant` | |
| `rated` | What was agreed, not what was asked: an acceptance is the recipient's half of the consent |
| `expires_at` | When the invitation stops being answerable. `null` on an acceptance, which has already been answered |
| `match_id` | The game acceptance produced. `null` on an invitation |

Two types share one shape for the reason `TournamentSummary`'s three do: an
invitation and its acceptance are two moments of **one** challenge, and two
payload classes would be two decoders, two wire keys and two client branches
for one noun.

**Never stored:** an email address, a private profile field, a token or
ticket, a Redis key, an internal identifier with no public meaning, a stack
trace, the raw source event payload, a bracket, a standings table, or an
entrant list. A payload carrying a field of 128 players would put a
tournament's whole state into every one of their inboxes.

The summary is composed through `PublicProfileComposer` for the recipient's
relationship at the moment of writing, so a field the actor withheld is not
in it. A pending request renders its sender at `STRANGER`, never `FRIEND` —
otherwise anyone could see a friends-only profile by sending a request
nobody has to accept.

---

## 4. Navigation targets

A **closed set of internal destinations**, as a type plus at most one safe
identifier. No URL is ever stored: an event-supplied URL would be an open
redirect written into a table, and a pre-rendered path would bake one
build's routing into rows that outlive it.

| Target | `ref` | Route |
| --- | --- | --- |
| `friend_requests` | `null` | `/friends/requests` |
| `friends` | `null` | `/friends` — **retired in A64-022.5**, kept because rows hold it |
| `challenges` | `null` | `/challenges` |
| `player_profile` | the actor's username | `/players/{username}` |
| `tournament` | the tournament's id | `/tournaments/{id}` |
| `live_game` | the match's id | `/games/{id}` |
| `match_replay` | the match's id | `/games/{id}/replay` |

`live_game` and `match_replay` are separate rather than one target the
client decides between: whether a game is still being played is a **server**
fact, and a client that guessed would send somebody to a live board that
ended yesterday. `live_game` acquired its first producer in A64-022.4 —
`friend_challenge_accepted`, whose whole point is reaching a board that
exists and has not started.

`friends` was a placeholder A64-022.4 wrote on received challenges because
no challenge surface existed. A64-022.5 built one, so `challenges` is what
new rows carry — and `friends` is **retired rather than deleted**: rows
written in the interval still hold it, and deleting the member would make
them raise on the way out. It is now readable and unproducible, which is the
correct end state for a target that was honest when it was written.

The client maps a target onto a route it already owns and renders anything
it does not recognise as a **non-navigable** notification. External
navigation is not merely forbidden in v0.x — no branch of the mapper can
produce a scheme.

A received request targets the list where it can be *answered* rather than
the sender's profile; an acceptance targets the new friend, because there is
nothing left to answer. A completed game targets its **replay**, because by
the time anybody reads the row the live room has nothing to show.

---

## 5. Exactly-once creation

`UNIQUE (recipient_id, source_event_id, type)`, and creation is
`INSERT ... ON CONFLICT DO NOTHING` against it.

Structural rather than checked. A redelivered event, a restarted relay and
two concurrent consumer processes all converge on one row without any of
them reading first — the reason it matters is that the failure it prevents
only happens when two ticks race, which is exactly when nobody is watching.

Narrower than `database.md` §10.2's original `(recipient_id, event_id,
category)`: `category` groups several types, so keying on it would let one
event produce one social notification and silently refuse a genuinely
different one from the same event.

There are **two** defences and they cover different failures:

| Defence | Covers |
| --- | --- |
| `platform.processed_event` | The relay redelivering a batch it already handled |
| The unique constraint | A crash between the notification write and the ledger write, and two consumer processes racing |

### 5.1 Fan-out shares one source event id — A64-021.4

Every recipient of one round publication carries that publication's outbox
id. The rows differ by `recipient_id`, so the constraint holds at 128 rows
exactly as it holds at one, and a redelivered publication inserts nothing
for anybody.

`type` stays in the key rather than being dropped as redundant. One source
event legitimately produces different types for different consumers, and a
narrower key would let the second one silently lose.

**No synthetic ids.** A consumer that generated its own event reference per
recipient would have no idempotency at all — the whole guarantee is that the
reference is the *source event's*.

### 5.2 Consumers are independent

`social_notifications`, `tournament_notifications` and `game_notifications`
each have their own `processed_event` partition. A redelivery one has
handled must still reach the others, and none may mark another's work done.

They also fail independently, which is the property that matters on a relay
tick: a tournament whose standings are not yet visible must not stall a
finished game's notification.

---

### 5.3 Failure behaviour — skip or retry, decided per cause

A64-021.4 §13. The rule is *retry what a retry could fix, skip what it
cannot*, and each case is named rather than left to a catch-all:

| Cause | Behaviour | Why |
| --- | --- | --- |
| A tournament the audience read cannot find | **Skip**, with a `WARNING` | A tournament that is gone will never be found, and retrying forever holds the relay's backlog open on a fact about something deleted |
| Standings not yet visible on completion | **Retry** | A genuine transient. Failing is the safe direction: a retry is bounded by the relay's attempt limit, where a skipped completion is a result nobody is ever told |
| A malformed payload — a missing id, an unparseable number | **Retry**, then exhaust | A producer that changed its contract without telling the consumer. §13's "prefer failing the batch when data integrity is wrong" |
| An outcome the game consumer does not recognise, or a match with no seats | **Skip** | An abort is a non-event and an unknown outcome is a backend that shipped ahead; neither is fixed by trying again, and both are safer as silence than as a guessed sentence |

The source aggregate's transaction **never** rolls back because a
notification failed: the event is committed with the fact that caused it and
the relay runs afterwards, which is AD-16's whole point.

---

## 6. Transaction boundary

One unit of work per batch, inside the consumer's **own** session — not the
relay's, which holds the claim and the ledger.

The notification row and the uniqueness that makes it exactly-once are the
same row, so they commit atomically by construction. The ledger commits
separately and afterwards, which is the right way round: a crash between the
two redelivers the event, and the redelivery finds the row already there.

**Nothing is delivered before the durable write commits.** A future realtime
frame is an optimisation over durable state, never a substitute for it.

---

### 6.1 Fan-out and batching — A64-021.4

A tournament event can name up to 128 recipients (`specs/tournament.md` §2).
Everything that could have been per-recipient is not:

| Step | Cost |
| --- | --- |
| Resolving the audience | **One** read, whatever the field size |
| Reading preferences | **One** read for the whole batch (§10.5) |
| Rendering opponents (`game_completed`) | **One** batch render per relay tick, not per match |
| Writing the rows | One insert per row, because each *is* a row |

**Measured: 6 `SELECT`s for a 128-recipient fan-out, and 6 for 16.** The
count does not move with the field. A per-recipient audience or preference
read would produce roughly 130, and
`tests/contract/test_notification_event_coverage.py` asserts a ceiling so
that a regression fails rather than merely getting slower.

Batch size is the relay's (`OUTBOX_BATCH_SIZE`, 50 by default) and is not
overridden here. What that bounds is *events per tick*, not recipients per
event: one publication is one entry, and its 128 inserts happen in one unit
of work — which is correct, because a partially-written fan-out and a
completely-unwritten one are both recoverable by the same redelivery, and
only the second is unambiguous.

---

## 7. Read state

Recipient-owned, and it touches nothing else. Marking a notification read
does not resolve a friend request, does not touch a friendship and tells
nobody — `notifications` holds no port that could reach a source aggregate.

| Operation | Behaviour |
| --- | --- |
| Mark one read | Idempotent. A second call keeps the original `read_at` and reports that it changed nothing |
| Mark all read | One statement over the unread partial index. Zero is a successful no-op |
| Mark unread | **Not implemented.** No product rule asks for it |

`read_at` is server time. A client's clock is not evidence of when anything
was read.

**Read state is not deletion.** A read notification stays in the list, and
§9's retention is independent of it.

---

## 8. HTTP API

Every route is authenticated and scoped to `CurrentUser`. **No route takes a
recipient id** — there is no parameter in which to ask for somebody else's
notifications.

| Route | Answers |
| --- | --- |
| `GET /api/v1/notifications?after&limit` | One page, newest first |
| `GET /api/v1/notifications/unread-count` | The badge |
| `POST /api/v1/notifications/{id}/read` | Marks one read |
| `POST /api/v1/notifications/read-all` | Marks every unread one read |
| `GET /api/v1/notifications/preferences` | The whole preference matrix — §10 |
| `PATCH /api/v1/notifications/preferences` | Changes what you receive — §10 |

There is **no route that creates a notification**, and that is structural:
`NotificationService` has no create method, and the only writer is a source
event's consumer.

### 8.1 Paging

Keyset on `(created_at, id)` DESC, cursor opaque and base64. Never `OFFSET`
— this is the one list on the platform where an insert *while the reader is
looking at it* is the normal case, so an offset would silently duplicate or
skip a row.

`limit` defaults to 20 and caps at 50. Lower than the platform's usual 100
because a notification row is taller than a history row and a client renders
every one of them.

### 8.2 Errors

| Code | Status | When |
| --- | --- | --- |
| `invalid_cursor` | 422 | A cursor this API did not issue |
| `not_found` | 404 | No notification with that id **belongs to this recipient** |
| `notification_preference_locked` | 422 | A change would mute a notification the platform must be able to deliver |
| `notification_channel_unavailable` | 422 | A change names a channel this build cannot deliver on |
| `duplicate_preference_change` | 422 | One request named the same `(category, channel)` twice |

The `404` is deliberately the same for "no such notification" and "somebody
else's". A `403` for the second would confirm the row exists, which is
enough to probe for other people's notifications one id at a time.

### 8.3 What the response never carries

`source_event_id` — it names a row in `platform.outbox`, a client has
nothing to do with it, and publishing it would let one correlate two
recipients' notifications back to a single social event. Nor any consumer
name, outbox id or stored object key.

### 8.4 Cost

| Operation | Statements |
| --- | --- |
| Create one notification | 1 (upsert), including the duplicate case |
| List a page | 1 |
| Unread count | 1, index-only, **no rows loaded** |
| Mark one read | 1 when it was unread; 2 when it was already read or is not the caller's |
| Mark all read | 1 |

Zero profile lookups per row, at every one of them.

---

## 9. Retention — append-only, and it is a limitation

There is **no retention for notifications**. Rows accumulate.

`platform.outbox` has a retention worker and a settings key;
`notifications.notification` has neither, and inventing one here would be a
cleanup with a horizon nobody has specified. `database.md` Q-15 records
retention-per-entity as an open question and R-24 says the answer belongs
beside the entity — so this is the entity, and the answer is not yet
written.

What is true today, stated rather than implied:

- Nothing is ever deleted, by any code path.
- **Read state is not coupled to deletion.** A read notification is kept.
- The table grows at social-event frequency per player, which is small — a
  player with a hundred friend requests a year accrues a hundred rows.

**The future task:** a retention horizon (90 days is the usual shape), a
prune that is a bounded batch rather than one `DELETE`, and a decision about
whether an unread notification past the horizon is dropped or kept. None of
it is started, and there is no partial mechanism pretending to be one.

---

## 10. Preferences — A64-021.3

**A player decides what reaches them, and the backend enforces it.** The
unit is a `(category, channel)` pair.

### 10.1 The matrix

| | `in_app` | `email` | `push` |
| --- | --- | --- | --- |
| `social` | on | — | — |
| `game` | on | — | — |
| `tournament` | on | — | — |
| `system` | **locked on** | — | — |

`—` means the channel does not deliver in this build. `email` is A64-021.5's
and `push` is A64-021.6's; the vocabulary exists now so that the day either
ships is a code change rather than a data migration, and so a settings
screen that showed one channel does not teach players that Arena64 has one.

**Defaults.** In-app **on**, because a notification list a player has to
switch on is a list nobody discovers, and the cost of being wrong is a row
they can mute in two clicks. Email and push **off**, because a stored `true`
on a channel that does not work would begin delivering to everyone who never
asked on the day it ships. *A channel arriving is not consent.*

**One lock, and the narrowness is the point.** `(system, in_app)` cannot be
switched off: `system` is account and security matters, and a player who
muted them would have no way to be told their account had been acted on.
Social, game and tournament notifications are **not** locked — nothing about
a friend request is essential, and a player who does not want them is
entitled to silence.

### 10.2 Storage — sparse

`notifications.notification_preference`, PK `(user_id, category, channel)`,
one row **per override**. A player who has never opened the screen has none
and is served entirely from the defaults above.

`database.md` §4.9 placed this relation in the `users` schema;
`domain-model.md` §9.3 said `notifications` owns no preference data. Both
are corrected — the vocabulary is this module's, the alternative is a module
cycle, and §9.3's actual rule (never a *second copy*) is unaffected by which
context owns the single one.

### 10.3 The read

`GET /notifications/preferences` returns **every** pair with its default
already resolved, each as four independent facts:

| Field | Says |
| --- | --- |
| `enabled` | What delivery does right now |
| `available` | Whether this build delivers on this channel at all |
| `editable` | Whether this player may change it |
| `locked_reason` | `essential`, `channel_unavailable`, or `null` |

The whole matrix rather than the stored overrides, so a client never
reimplements the defaults and the two cannot drift. `available` is a
**backend** fact and is deliberately separate from browser capability: a
browser with `PushManager` still receives nothing, because nothing is sent.

### 10.4 The write

`PATCH /notifications/preferences` with `{"changes": [...]}` — only the
switches that moved, so a save cannot overwrite a category the client never
rendered.

**Validated whole, then written whole.** Every change is checked before any
is written; one illegal change rejects the request and the table does not
move. A batch naming the same pair twice is refused rather than resolved by
last-write-wins, because it has no intent.

The response is the resulting matrix — exactly what a fresh `GET` would say
— so a save is one request and the screen cannot disagree with the server.
An empty change list is a legal no-op.

Rate-limited per authenticated user (30 per 5 minutes). The read carries no
limit: it is one indexed read of the caller's own rows.

### 10.5 Enforcement — creation, not filtering

**NT-4 honoured literally.** `NotificationDeliveryPolicy` is asked at
*delivery* time, inside `DurableNotificationWriter`, before the unit of work
opens. So somebody who mutes a category between the friend request and the
relay tick that carries it has muted it.

A muted category means: **no durable row, no realtime frame, no change to
the unread count.** Not a row written and hidden — a hidden row is a record
the player never consented to sitting in a table they cannot see, and the
first reporting query would find it.

One query per **batch**, not per recipient: the policy takes a sequence and
answers a set, so a future tournament fan-out is one indexed read rather
than one per entrant.

### 10.6 What A64-021.3 deliberately does not do

No email sending, no templates, no SMTP. No push subscription, no VAPID key,
no service-worker push handler, no browser permission request. No delivery
attempt tracking, no digests, no quiet hours, no per-type granularity below
the category. Each is named in the phase that owns it.

---

## 11. Realtime delivery — A64-021.2

HTTP is the source of truth. Realtime is an **accelerator**, and every rule
below exists to keep that sentence literally true.

    friend request                 an HTTP call, returning before any of this
      ↓
    outbox                         durable in the same transaction
      ↓
    relay → SocialNotificationDispatcher
      ↓                            audience re-read, privacy gate
    CompositeNotificationSink
      ├─ DurableNotificationWriter → notifications.notification  (commits)
      │     ↓ after the commit, only what it inserted
      │  GatewayNotificationSink  → RoomBroadcaster → recipient's socket(s)
      └─ LoggingNotificationSink
      ↓
    client: invalidate the two notification queries
      ↓
    HTTP re-read decides everything

### 11.1 The frame

`notification.created`, on the `notifications` channel, addressed to the
recipient and to nobody else.

| Field | Why it is allowed |
| --- | --- |
| `notification_id` | The client's duplicate guard needs an identity |
| `type` | Lets a future surface filter without a read |
| `created_at` | Lets a future surface order without a read |

**Nothing else.** No actor, no username, no display name, no avatar, no
rendered sentence, no target, no token, no email, no internal identifier and
no URL. A pushed payload is a second copy of a record the client is about to
fetch, and a second copy is a second thing that can be stale, be wrong, or
leak. `recipient_id` is the *address* and never reaches the wire — a client
already knows who it is.

The channel is new and the protocol version is not: an unknown channel reads
as `system` and an unknown type is ignored, so both halves of the fleet can
be mid-deploy without a client breaking.

### 11.2 Ordering — after the commit, and only what was written

`DurableNotificationWriter` announces **after** its unit of work commits, and
only for the rows `append` actually inserted.

*After*, because a client woken by the frame reads `GET /notifications`
immediately; announcing inside the transaction would let a fast client read
before the commit landed and conclude nothing was there — the one race that
would make "HTTP is authoritative" a liability rather than a guarantee.

*Only what was written*, because a redelivered event inserts nothing and
therefore announces nothing. The client's own duplicate suppression is a
second line, not the only one.

### 11.3 Failure is always tolerable

`NotificationAnnouncer` **never raises**, which is a deliberate departure
from `NotificationSink`'s "a sink may raise". A retry would re-announce
something the client can already read, and raising would fail a relay tick
whose durable work is already done.

| Failure | What happens |
| --- | --- |
| Nobody connected | Counted as `no_connection`. The ordinary state of a player who is not looking at the app |
| The socket dropped | The frame is lost; the next read recovers it |
| The recipient is on another node | Forwarded through the existing bus — §11.4 |
| The fan-out raised | Counted as `failed`; the rest of the batch still goes |
| The client never got it | Nothing is lost. The row is durable and the badge is a `GET` away |

### 11.4 Cross-node

Unchanged, and that is the claim. `RoomBroadcaster` partitions recipients
into local sockets and remote nodes through `FleetConnectionRouter`, and the
remote half is published to the node's bus stream where its `GatewayForwarder`
delivers it. The notification path uses the same fan-out as moves and match
offers; it introduces no transport of its own.

### 11.5 The client half

One handler on the **one shared socket** — no second connection, no
notification-specific socket, no local pub/sub, no `BroadcastChannel`.
Mounted by `AppShell`, so it is alive on every route.

It reads exactly one field, the id, to tell news from a duplicate. Then it
**invalidates two query keys and does nothing else**. It never renders the
payload and never mutates a count, which is what makes a late frame
harmless: a notification already read stays read, because the refetch says
so.

Duplicates collapse twice over — an id already reconciled is dropped, and
several distinct ids arriving together share one refetch.

### 11.6 Polling is not removed

The badge still refetches on focus and the list on its own terms. A build
whose socket never connects is exactly the product A64-021.1 shipped; §11.3's
table is the whole of the fallback, and there is no code path to take.

### 11.7 Cost

| Measurement | Value |
| --- | --- |
| Server-side latency, request accepted → frame published | ~1 s, bounded by the outbox relay's 1.0 s poll interval; the push itself is within the same log second |
| Client requests caused by one frame | 2 — the list and the count, once each |
| Client requests caused by 3 duplicate frames | 0 |
| Polling frequency | unchanged |
| New sockets, endpoints or connections | 0 |

Before this phase the same notification became visible when the tab next
regained focus, which is unbounded.

---

## 12. Extension points

Everything below is deliberately absent, with the seam it will use.

A64-021.2's, A64-021.3's and A64-021.4's rows are gone from this table
because they were built. The seam each used — a port satisfied at the
composition root — is the same one every remaining row names.

A64-021.4 is the clearest demonstration: two new consumers, a published
reader on `tournament`, one additive event, and **no change** to the
transport, the preference policy, the exactly-once key or the wire frame.

A64-021.5's email channel is gone from it too. What it took was the seam
this table predicted — a published recipient reader on `users`, a durable
delivery queue, and the same `NotificationDeliveryPolicy` asked with
`channel=email`. §13 is what it looks like when taken.

A64-021.6's push channel is gone as well, and it is the strongest evidence
the seam holds: a second channel, a second transport, a second delivery
table and a browser permission flow, with **no change** to the notification
record, the exactly-once key, the realtime frame or the preference model.
The one prediction it did not keep is where the VAPID key lives — this table
guessed a `VITE_` variable, and §15 says why the *private* half must never
be one.

| Deferred to | What it adds | Where |
| --- | --- | --- |
| **Friend challenges** | A `challenges` domain and its two events. Everything downstream of the event already exists — see §15.16 for the exact seam | A new member of `NotificationType`, its payload, its target, and one row in each of four tables that already exist |
| **`tournament_match_ready`** | An event. `TournamentMatchLauncher` holds no publisher, `game.match_activated` carries no `origin`, and `launch()` does not report whether an attempt was newly recorded — without the third, a re-launch after a restart notifies twice | `tournament.application.services.match_launcher`, and one more member of `tournament.public`'s event re-exports |
| **`tournament_cancelled`** | A publisher. The event is declared and no service emits it | `tournament.application.services` — wherever cancellation is eventually implemented |

The worker's message contract stayed as narrow as it was. A64-021.6 added
`push` and `notificationclick` listeners and **no** new `message` branch: a
page still cannot tell the worker to do anything but skip waiting.

---

## 13. Email — A64-021.5

The second channel. In-app delivery is the record; email reaches somebody who
is **not looking at Arena64**, which is its whole value and its whole cost.

### 13.1 Which types, and why only three

| Type | Email | Why |
| --- | --- | --- |
| `tournament_registration_confirmed` | **yes** | A commitment to be somewhere at a time the platform chose. The receipt is what a player looks back for |
| `tournament_round_published` | **yes** | The one notification whose value *decays* — a round published while somebody is away is the case email exists for |
| `tournament_completed` | **yes** | The final result, with the recipient's own placement |
| `friend_request_received` | no | Answered by a button in the app, and not time-critical. It is also the type an **abuser controls the rate of** — an email per request would make the inbox a harassment surface the block list does not reach |
| `friend_request_accepted` | no | Pleasant, and nothing follows from it |
| `game_completed` | no | The player was at the board. The cases it exists for — an adjudication, a flag on a closed tab — are real but rare, and one email per game is the wrong price for them |

**A preference is necessary and not sufficient.** Enabling tournament email
must not sign a player up for one message per round per player per bracket,
so a type must be email-capable *and* the category unmuted on the channel.

### 13.2 Opt-in, deliberately

Every non-in-app channel defaults to **off** (§10.1), and A64-021.5 did not
change it. A64-021.3 told players email was unavailable; flipping the default
would have started emailing every one of them the day a provider was
configured — which is the exact scenario that paragraph warned about.

The channel therefore ships quiet, and that is the correct direction to be
wrong in: a player who wanted email and must enable it is mildly
inconvenienced; a player who did not and receives it has been emailed without
consent.

### 13.3 Only verified addresses

`users.public.EmailRecipientDirectory` answers *"may we email these people,
and where"*. Eligibility is **the absence of a result**, not a flag:

| Cause | Result |
| --- | --- |
| No such account, address missing, unverified, or account deactivated | Absent from the answer, and the delivery records `skipped_no_email` |

Collapsing four causes into one absence is deliberate — a consumer that
could tell "no such account" from "unverified" would be an
account-existence oracle, and none of the four changes what a delivery does.

The address is resolved **at delivery time** and never stored on a
notification or a delivery row, so a change of address between enqueue and
send reaches the right inbox and the delivery table is not a list of email
addresses.

### 13.4 The delivery queue

`notifications.notification_email_delivery`, `PRIMARY KEY (notification_id)`.

| Property | How |
| --- | --- |
| Durable | A row, written in the notification's **own transaction**. §9: an in-process task lives on one node and a deploy takes it with it |
| Idempotent | The primary key *is* the identity — `INSERT ... ON CONFLICT DO NOTHING`, and a retry reuses the row rather than creating one |
| Claimed, not read | One `UPDATE ... WHERE id IN (SELECT ... FOR UPDATE SKIP LOCKED) RETURNING`, so two workers never send the same message |
| Recoverable | A worker that dies mid-send leaves an unresolved claim; `reclaim_stale` returns it to the pool after ten minutes |
| Bounded | `last_error_code` is an `EmailDeliveryOutcome` this platform chose, never vendor text. No subject, no body, no address |

**Nothing is rendered at enqueue time.** A frozen body would be sent in
whichever locale the recipient had *then*, and would keep being sent after a
template fixed a mistake in it.

### 13.5 Outcomes, retries and failure

Every expected result is an **outcome**, never an exception (§6):

    delivered                     a provider accepted it
    skipped_preference            the category is muted on email
    skipped_unsupported_type      the type is not email-capable
    skipped_no_email              ineligible recipient — see §13.3
    skipped_channel_unavailable   this process cannot send at all
    retryable_failure             a fault that may not recur
    permanent_failure             a fault that will
    attempts_exhausted            retried to the limit and never accepted

Retries are exponential and capped — 1m, 2m, 4m, … to six hours, five
attempts by default — mirroring the outbox relay's schedule rather than
inventing a second one. `attempts_exhausted` is distinct from
`permanent_failure` because they mean different things to an operator: a bad
address versus a provider that was down for hours.

**The classification belongs to the adapter.** Only it can read a vendor's
status code, so it raises `PermanentEmailFailure` for a rejection that will
recur and lets everything else propagate — and anything unclassified is
treated as **retryable**, because an unknown fault is more likely transient
and the attempt limit bounds the cost of being wrong.

**Email never affects the record.** A provider that is down produces
retryable rows and an in-app list that is already correct: the notification,
the realtime frame and the source action all committed before a worker could
run.

### 13.6 The provider — Resend

**Arena64's production transactional email provider is Resend**, sending
from `no-reply@arena64.gg` on a domain already verified with them. One
transport carries everything this platform sends: the verification link, the
password reset, and notification email.

| Setting | Value | Note |
| --- | --- | --- |
| `RESEND_API_KEY` | *(secret)* | Server-side only. `SecretStr`, so it cannot reach a log or a traceback through a repr. Never a `VITE_` variable, and never committed |
| `EMAIL_FROM_ADDRESS` | `no-reply@arena64.gg` | The verified domain. An address outside a domain with SPF, DKIM and DMARC records for Resend is rejected, which this platform records as a **permanent** failure and stops retrying |
| `EMAIL_FROM_NAME` | `Arena64` | The display name beside it |
| `PUBLIC_APP_URL` | `https://arena64.gg` | The canonical frontend origin — **one** setting, used by every email link. Refused at startup in a deployed tier while it is the localhost default |

**The credential is the switch**, and there is deliberately no second flag
saying "email works":

    key set    `ResendEmailProvider` is built and the channel reports
               itself available
    key unset  `ConsoleEmailProvider` is built — and it **refuses to
               construct in a production-like tier**, so a deploy that
               forgot the credential fails at boot rather than accepting
               registrations nobody can verify

`NOTIFICATION_EMAIL_ENABLED` remains, and is a *kill switch* rather than the
gate: a way to stop notification mail without withdrawing the credential
that verification and reset mail also depend on. Both are composed in one
place — `email_channel_available` — so a settings screen and a delivery
worker cannot disagree.

#### Why a narrow HTTP adapter rather than the SDK

`ResendEmailProvider` posts one JSON body to `POST /emails` through `httpx`.
The `resend` SDK was weighed and not taken: its `send` is synchronous, which
in a worker whose entire job is I/O means blocking the event loop or a
thread per message; it exposes no per-request timeout, which §2 requires;
and the API it wraps is one endpoint with five fields. `httpx` was already
this repository's HTTP client, so no new vendor arrives — CLAUDE.md §2.6.

Nothing about Resend appears in a signature, a return type or an exception
outside that one file.

#### Verifying a deployment

    python -m app.operator.notification_email smoke --to you@example.com

Sends **one** fixed message to an address the operator types — never a
notification, never a stored recipient, never a default address, and never
from pytest, HTTP or startup. It reports the provider's message id, so the
send can be found in Resend's dashboard rather than only in an inbox.

Automated tests never contact Resend: the adapter's suite stubs the HTTP
boundary with `httpx.MockTransport`, and the delivery suite substitutes the
provider port.

### 13.7 Template safety

Rendered from the typed payload, in the recipient's stored locale (uz, ru,
en), as **both** a plain-text and an HTML part — §17 forbids HTML-only.

| Rule | How |
| --- | --- |
| No injection | Every interpolation is `html.escape`d at the interpolation site. The text part is deliberately **not** escaped — `Bob &amp; Sons` in a text client is the same bug pointing the other way |
| No template engine | Each template is a Python function, so no payload string can become a placeholder |
| No arbitrary URL | Every link is the configured origin plus a path built from an identifier. No branch concatenates a caller-supplied string, so nothing can produce a scheme or a `javascript:` target |
| No token in a URL | §18. The call to action is the tournament; the preference link is `/settings/notifications`, behind a session. A tokenised one-click unsubscribe would be a bearer credential in a mailbox |
| No tracking | No pixel, no external stylesheet, no script, no image |

### 13.8 Operations

    python -m app.operator.notification_email status

Counts by status, and **nothing else** — no recipient, no address, no
notification id. An operator can learn the channel is healthy and cannot
learn who was emailed. There is no resend, no flush and no "send this one
now": §20 makes notification email server-controlled, and a command that
could send an arbitrary message is the capability an attacker would want
from a compromised shell.

One metric, two closed labels: `notifications.email.deliveries{type,outcome}`.
No address, no user id, no notification id, no provider message id — §22.

Logs carry the notification type, the outcome and counts. Never a recipient,
a subject, a body, or a provider's response — and a provider exception is
**not** logged with `exc_info`, because its message can contain an address.

---

## 14. Security

| Guarantee | How |
| --- | --- |
| The recipient is the token, never a parameter | No route accepts a recipient id |
| Another player's notification cannot be read or marked | Every repository method is recipient-scoped; there is no `get(id)` |
| Absence and refusal are indistinguishable | One `404` for both |
| No private profile field is stored | The payload is composed through `PublicProfileComposer` |
| No token, ticket or Redis key is stored | The payload is five typed fields |
| No arbitrary URL is stored or followed | Targets are a closed enum plus one identifier |
| No HTML injection | No server string is rendered as markup; the client composes every sentence from translations |
| No cross-user cache leakage | Query keys carry no player id because the endpoints take none; sign-out clears the cache |
| A source event cannot notify an unrelated recipient | The recipient is derived from the event's own participants, or from a `tournament.public` read, and never from a client |
| A fan-out cannot reach somebody who left | The audience query filters on `status = REGISTERED` at delivery time; a completion reads `standing`, which a withdrawal never produces |
| A consumer cannot change what it reads about | `tournament.public.TournamentNotificationReader` publishes **two reads and no write** — a compromised consumer could learn a field and could not enter, withdraw, pair or finish anybody |
| A match id in a notification grants nothing | `/games/{id}` and `/games/{id}/replay` authorize on their own; the notification is a pointer, not a capability |
| A preference cannot be bypassed by a new producer | Every producer goes through `DurableNotificationWriter`, and the policy is a constructor argument with no default |
| Only a verified address is emailed | `EmailRecipientDirectory`'s query filters on `is_verified`, `is_active` and a non-empty address. It is the `WHERE` clause, not a flag a consumer reads — the first one to forget it cannot exist |
| A caller cannot name an address | The directory takes **ids**. There is no API, no payload field and no operator command through which an address is supplied, and none is stored on a notification or a delivery |
| Provider credentials are server-side only | `RESEND_API_KEY` is a `SecretStr` read at the composition root. Nothing about it is a `VITE_` variable, nothing about it reaches a response, and no log line carries it — not even a length or a prefix |
| A provider's own words never escape | The adapter raises a typed exception carrying a status code. A Resend error body can quote the address it rejected, so none of it is logged, persisted or returned |
| No token is ever in an email URL | Every link is the configured origin plus a path built from an identifier. The preference link is a page behind a session, not a one-click unsubscribe token |
| Logs and metrics carry no address | The metric's two labels are closed enumerations; the log lines carry a type, an outcome and counts. A provider exception is logged **without** `exc_info`, because its message can contain an address |
| The delivery table is not exposed | No route reads it. The operator command returns counts by status and cannot name a recipient |
| A caller cannot subscribe another account | The account comes from the session; there is no `user_id` field, and `extra="forbid"` makes supplying one a `422` |
| A push endpoint cannot be enumerated | No method reads by an endpoint a caller supplies except the owner-scoped removal, which answers identically for an endpoint that is not theirs and one already gone |
| A subscription is never returned to a client | Registration answers an id. The endpoint and both keys appear in no response, no log and no metric |
| A previous account cannot be pushed on a shared browser | Two independent mechanisms: the endpoint upsert re-binds ownership atomically, and signing out unsubscribes the browser and revokes the row before the session ends |
| A push payload carries nothing private | Two identifiers. The text is a closed table compiled into the service worker; no body, name, address or token travels |
| A push cannot navigate anywhere | Every destination is a literal path in that same table. No branch concatenates a payload value into a URL, and the worker refuses anything that does not resolve to its own origin |
| The VAPID signing key is server-side only | `SecretStr` read at the composition root. Never a `VITE_` variable, never in a response, never logged. The **public** half is served, because a browser cannot subscribe without it |
| A stored endpoint cannot become a request to an arbitrary host | The boundary refuses anything that is not an absolute `https` URL, in the domain rather than only in a schema |
| A malformed key cannot be stored | 65 and 16 bytes exactly, enforced at the boundary and by the column. A key of any other length can never encrypt anything |
| A push service's own words never escape | The transport raises a typed exception carrying a status code; the error body is never logged, persisted or returned |
| The subscription API is not cached by the service worker | `cache-policy.ts` classifies everything on `/api` as `"network"` — unchanged, and it already covered these routes the day they were added |

---

## 15. Web Push — A64-021.6

The third channel. In-app delivery is the record and email reaches somebody
who is away; push reaches somebody whose browser is closed, within seconds,
and is the only channel that can interrupt.

That is also why it is the most restricted: a push is an interruption, and
the cost of getting it wrong is that people switch the channel off — not one
category, the channel.

### 15.1 What is pushed

`domain/push.py`'s `PUSH_CAPABLE_TYPES`: the same three tournament types the
email channel sends.

| Pushed | Why |
| --- | --- |
| `tournament_registration_confirmed` | Confirmed after the tab was closed |
| `tournament_round_published` | Published while the player is away; time-critical |
| `tournament_completed` | Finishes overnight |

| `friend_request_received` | A64-021.6A. Somebody is waiting: a request nobody sees is a request that expires |
| `friend_request_accepted` | A64-021.6A. The moment two people can actually play |
| `friend_challenge_received` | A64-022.4. Somebody is waiting, with a clock: the invitation expires in twenty-four hours |
| `friend_challenge_accepted` | A64-022.4. A match now exists and both players must join it inside ten minutes |

| Not pushed | Why |
| --- | --- |
| `game_completed` | The player is looking at the result screen. Pushing it notifies them of something they are already reading |

Push defaults to **off** for every category (`_default_for`: in-app on,
everything else off). A channel that interrupts has to be asked for.

#### The friend types, and the concern that deferred them

A64-021.6 left the two social types out on the grounds that a friend request
is attacker-controllable — anybody can send one, so a pushable type is one a
stranger can use to make somebody's phone buzz.

A64-021.6A did not overrule that concern; it found the bound already in
place, three times over and none of it push-specific:

| Control | Effect |
| --- | --- |
| `friend_request_send_user`, 20/hour **per sender** | A stranger gets twenty buzzes an hour across the whole platform, not twenty per victim |
| Blocking | A blocked player cannot send a request at all — the recipient's own control removes the sender entirely |
| Duplicate refusal | A second pending request to the same person is refused, so a sender cannot repeat |

And the one that settles it: **a push exists only where a durable
notification does.** Every rule above runs before the notification is
written, so a request that is rate limited, blocked, duplicate or malformed
produces no notification and therefore no delivery row. There is no path from
a refused request to a push, and no push-specific bypass was added.

What remains is a stranger's twenty an hour — the same volume the in-app
badge and the realtime frame have always carried — and a person who does not
want it mutes the `social` category, which the matrix has offered since
A64-021.3.

#### The challenge types — A64-022.4

The same argument, and the bound is **tighter**: a challenge can only be sent
to a friend, only one may be live per unordered pair, and
`challenge_create_user` caps the rate. A stranger cannot reach either type at
all, so the abuse surface the friend types had to argue about does not exist
here.

Email is **not** enabled for either. Nothing about a challenge decays over
hours the way a tournament round does — it lives for twenty-four — and the
channel that matters is the one that reaches a phone in seconds.

### 15.2 Subscriptions

`notifications.push_subscription`, one row per **browser** — not per person
and not per device. A laptop with two browsers is two subscriptions and a
browser whose site data was cleared becomes a new one, because the Push API
offers no device identifier and inventing one would be an identity nothing
can maintain.

| Column | Note |
| --- | --- |
| `endpoint` | The push service's URL. **Unique across live and revoked rows** — this is the ownership rule, see §15.3 |
| `p256dh`, `auth` | The browser's public key (65 bytes) and auth secret (16), exact lengths fixed by RFC 8291 |
| `last_seen_at` | Written on every re-registration. The only "is this device still real" signal available — a push service reports nothing until a delivery fails |
| `revoked_at` | Set, never deleted, so an operator asking *why did this device stop* gets an answer |

The three fields together are a **bearer capability**: anybody holding them
can push to that browser, subject to VAPID. They are never returned to a
client, never logged, and never accepted as a lookup key a caller supplies.

### 15.3 Ownership, and the shared-browser leak

`POST /notifications/push/subscriptions` derives the account from the
**session**. There is no `user_id` field, and `extra="forbid"` makes an
attempt to supply one a `422` rather than a silently ignored field.

Registration is `ON CONFLICT (endpoint) DO UPDATE`. A browser re-subscribing
keeps working; a browser whose endpoint now belongs to a different account
is **re-bound**, in one statement, with no window in which two rows claim
it. That is not a convenience — it is the leak defence:

    A signs in on a shared laptop, enables push, signs out
    B signs in on the same browser and enables push
    → the endpoint is re-bound to B, and A can no longer reach it

The other half — B never enables push — is closed on the client. Signing out
unsubscribes this browser and revokes the row, through `onSessionEnding`,
**before** the session is revoked (the removal needs it). Both halves are
required: the upsert alone leaves A's subscription live until B opts in, and
the sign-out alone leaves it live if A never signs out.

### 15.4 Delivery

`notifications.notification_push_delivery`, keyed
`(notification_id, subscription_id)` — one row per notification **per
device**, which is the whole difference from the email table. Each is
claimed, attempted and settled independently, so one dead device does not
prevent the others.

The pipeline is the email one's:

    durable notification (committed)
      → push delivery rows, in the same transaction, fanned out over the
        recipients' live subscriptions in one query
      → worker claim (`FOR UPDATE SKIP LOCKED`, `attempt_count` incremented
        by the claim)
      → availability, type, preference, subscription
      → encrypt, POST, classify
      → record; revoke the subscription if it is gone

Enqueueing is `ON CONFLICT DO NOTHING` on the pair, so a redelivered source
event pushes nothing twice — §19's idempotency as a constraint rather than a
check somebody remembered.

The preference is read at **delivery** time, so somebody who mutes push
after a round is published is not pushed.

### 15.5 The payload, and what a lock screen says

Three fields at most:
`{"n": <notification id>, "t": <type>, "r": <navigation ref>}`, short keys
because the encrypted envelope has a fixed 86-byte overhead against a ~4 KB
ceiling. `r` is omitted entirely when the type's destination is a list,
which is every type but one.

Absent, deliberately: the notification body, any display name, any address,
any token, and any URL. A push payload lands in a browser's notification
store on a device that may be shared, and shows on a lock screen.

`r` is the **`ref` of the notification's own `NavigationTarget`** (§4), read
by the delivery worker in one batched, recipient-scoped query — not a path
and not a URL. The worker still owns every route: it maps a *type* to a path
shape and substitutes this value into it, and it accepts a UUID and nothing
else, so a malformed ref falls back to the list rather than contributing a
path segment. Today the only value it carries is a match id, for a match the
recipient is already a player in.

The **text** is a closed table compiled into the service worker
(`pwa/push-presentation.ts`) — §12's approach B, and every `path` in it comes
from `shared/config/notification-routes.ts`, the same constants the in-app
list resolves through. Two copies of a route is a push whose text says
"friend request" and whose tap opens a different page.

The two social entries are stricter than the tournament ones: the title is
the product name and the body names nobody.

| Type | Lock screen | Opens |
| --- | --- | --- |
| `friend_request_received` | *Arena64* — "You have a new friend request." | `/friends/requests` |
| `friend_request_accepted` | *Arena64* — "Your friend request was accepted." | `/friends` |
| `friend_challenge_received` | *Arena64* — "You have a new game challenge." | `/challenges` |
| `friend_challenge_accepted` | *Arena64* — "Your game challenge was accepted." | `/games/{match_id}` |

A tournament notification discloses that somebody plays in tournaments; a
social one would disclose **who is trying to reach them**, on a lock screen,
to whoever is looking at it. The sender's name is one tap away, behind the
session — and the in-app notification still shows it in full. Server-composed text was
rejected because it is *specific* text: "Round 3 is live in the Tashkent
Open" names a tournament somebody is in, in public. "A new round is live"
discloses that this person uses Arena64 and nothing more.

The text is English and not translated. A service worker has no i18n
runtime and no access to the language the person chose;
`navigator.language` is the operating system's preference, which is
frequently a different language, and a notification in the wrong language is
worse than one in a consistent one. The full notification, in their own
language, is one tap away.

### 15.6 `notificationclick`

Close, then focus an existing tab and navigate it, or open one. The
destination comes from the **same closed table** — every entry is a literal
path, so there is no branch that can concatenate a payload value into a URL.
The worker resolves it against its own origin and refuses anything that does
not stay there.

Every route was a list (`/tournaments`, `/notifications`) rather than an
item, because the payload's id names a *notification* and not a tournament —
`/tournaments/{notification id}` would open something that does not exist.

A64-022.4 added the one exception, and it is not a relaxation of that rule
but its resolution: `friend_challenge_accepted` carries an identifier that
names the **thing itself**, so the worker can open `/games/{match_id}`. It
still concatenates nothing from the payload into a URL — the path shape is
compiled in and the substituted value must be a UUID. A ref that is absent,
empty or malformed falls back to `/notifications`.
A protected route may bounce to `/login` and back, unchanged by this being a
push.

### 15.7 VAPID

| Variable | Note |
| --- | --- |
| `VAPID_PUBLIC_KEY` | **Not a secret.** Handed to every browser that subscribes and served by `GET /notifications/push/status` |
| `VAPID_PRIVATE_KEY` | Server-side only. `SecretStr`, never a `VITE_` variable, never in a response, never in a log |
| `VAPID_SUBJECT` | `mailto:no-reply@arena64.gg`. A contact address for a push service operator; the one field safe to log |

Generate with `python -m app.operator.push_keys generate`. Never at startup:
a browser commits to the public key when it subscribes, so **changing the
pair invalidates every existing subscription immediately and permanently**,
and a platform that generated one when it could not find its configuration
would do that silently on a restart. The symptom is push ceasing to work for
everybody who had enabled it, with no error anywhere.

An absent pair is allowed and reports the channel unavailable; a **present
but mismatched** pair raises at startup, because accepting it binds every
subscription created afterwards to a key that cannot sign for them.

### 15.8 Availability

`can_deliver_push(settings)` — does this process hold a VAPID pair. There is
no `PUSH_ENABLED`, because a boolean beside the keys can disagree with them,
and the player is the one who finds out.

A server without one answers `available: false` with a `null` key, refuses
to store a subscription, and the settings screen says so.

### 15.9 Error classification and retry

| Push service says | Outcome | Effect |
| --- | --- | --- |
| `404`, `410` | `subscription_gone` | The subscription is **revoked**. No retry — this is the ordinary end of a browser's life |
| `429`, `5xx`, timeout, connection error | `retryable_failure` | Exponential backoff, capped, to `PUSH_MAX_ATTEMPTS` |
| other `4xx`, unusable stored key | `permanent_failure` | Abandoned. Retrying asks the same question |
| — | `skipped_preference` / `skipped_unsupported_type` / `skipped_no_subscription` / `skipped_channel_unavailable` | Not sent, and nothing was wrong |

Backoff is the email channel's, identical rather than a second schedule
(§18), through the same `PeriodicTaskScheduler`.

### 15.10 API

| Endpoint | Auth | Limit |
| --- | --- | --- |
| `POST /notifications/push/subscriptions` | `VerifiedUser` | `push_subscription_user`, 60/hour |
| `POST /notifications/push/subscriptions/remove` | `CurrentUser` | the same bucket |
| `GET /notifications/push/status` | `CurrentUser` | none — one indexed read of the caller's own rows |

Registration requires a **verified** address (§24, aligning with
A64-021.5H): an unverified throwaway account could otherwise accumulate
endpoints this platform POSTs to on a schedule. Removal deliberately does
**not** — it is the path a sign-out takes, and a person whose verification
lapsed must still be able to stop their devices being pushed to. Removing a
capability is never the operation to gate.

`POST .../remove` rather than `DELETE`, because the endpoint has to travel
and both `DELETE` shapes are worse places to put a bearer capability: a URL
lands in access and proxy logs, and a `DELETE` body is stripped by enough
intermediaries not to be dependable.

### 15.11 Transport

RFC 8030 over `httpx`, RFC 8291 encryption and RFC 8292 VAPID implemented
against `cryptography` — one new dependency rather than `pywebpush`'s four,
and the deciding factor is the one that ruled out the Resend SDK: its
`webpush()` is synchronous, in a worker whose entire job is I/O, with no
per-request timeout.

Nothing about a vendor appears above `app/platform/push/`. The transport is
endpoint-blind: no code parses an endpoint or branches on its host.

### 15.12 Metrics and logs

    notifications.push.deliveries{type, outcome}

Both labels are closed enumerations this platform defines. **No** endpoint,
user id, notification id or subscription id — every one is unbounded
cardinality and three are fed by a third party.

Log lines carry counts, outcomes and a subscription id at most. No endpoint,
no key, no recipient. A push service's error body is never logged: it can
quote the endpoint it rejected.

### 15.13 Performance

| Case | Cost |
| --- | --- |
| Fan-out, 1 recipient × 1 device | 1 subscription query, 1 insert, 1 POST |
| Fan-out, 1 recipient × 3 devices | 1 subscription query, 1 insert (3 rows), 3 POSTs |
| Tournament round, 128 players × 2 devices | **1** subscription query (`user_id IN (...)` over a partial index), **1** insert of 256 rows, then 256 POSTs across 13 worker passes at `PUSH_BATCH_SIZE=20` |

No per-device user lookup and no per-notification query: `live_for_many`
exists precisely so the fan-out inside the notification's transaction is one
round trip. The worker reads subscriptions once per pass, not once per row,
and never loads the notification — the payload is two identifiers the
delivery row already carries.

### 15.14 Testing, and its limits

| Layer | Covers |
| --- | --- |
| `tests/unit/test_web_push_protocol.py` | RFC 8291 **decrypted as a browser would**, and a VAPID assertion verified against the public half. Both failures are otherwise silent |
| `tests/contract/test_push_notifications.py` | The real API and worker against a **stub push service**: ownership, account switch, availability, delivery, `410`, retry, preference, idempotency |
| `pwa/push-presentation.test.ts` | The closed text and route tables |
| `src/features/notification-push/push.test.tsx` | The settings states and the enable/disable flow, over a mocked `PushManager` |

**No test proves external Web Push delivery, and none can.** No real push
service is contacted anywhere in this suite (§28). What is established is
that the bytes are the ones RFC 8291 specifies, that every status code a
real service can answer with leads to the right state, and that the browser
flow calls what it should in the order it should.

### 15.15 Friend challenges — the seam, and nothing else

**Not implemented, and no placeholder code exists.** What follows is where
`friend_challenge_received` and `friend_challenge_accepted` will plug in, so
that the phase which builds them does not have to rediscover it.

The point of writing it down is that the answer is *small*: A64-021.6A added
two notification types to the push channel and touched one line of the
delivery pipeline, which is zero. A challenge type is the same shape.

| Step | Where | What it takes |
| --- | --- | --- |
| 1. The events | **`matchmaking`** — `domain-model.md` §10.3 places `Challenge` beside `QueueTicket`, corrected by A64-022.1; the events exist as of that phase and nothing publishes them yet | Wiring the publisher is one constructor argument on `ChallengeService` |
| 2. The types | `notifications/domain/record.py` | Two members of `NotificationType`, two entries in `CATEGORY_OF` — both `SOCIAL`, which already exists in the preference matrix and already has a switch per channel |
| 3. The target | `notifications/domain/record.py` | One member of `NavigationTargetType`. A challenge has an identifier, so it is a `ref`-carrying target like `tournament` |
| 4. The consumer | `notifications/application/services/` | A dispatcher beside `SocialNotificationDispatcher`, subscribing to the two events and composing a `NotificationRecord` |
| 5. Push | `notifications/domain/push.py` | Two members of `PUSH_CAPABLE_TYPES`. **Nothing else on the backend.** The delivery table, the fan-out, the worker, the retry policy, the idempotency key and the revocation path are all type-agnostic |
| 6. Push text and route | `apps/web/pwa/push-presentation.ts` | Two entries. A destination that has no identifier goes in `shared/config/notification-routes.ts`; one that does is a list page, for the reason §15.6 gives — a push payload names a *notification*, not a challenge |
| 7. In-app route | `features/notifications/model/navigation.ts` | One `case`, resolving the new target type through the same constants |

What it does **not** take, and this is the part worth stating: no migration,
no new table, no change to `ChannelAvailability`, no change to the preference
model, no new rate-limit rule, no service worker listener, and no change to
the payload contract. A type this build has never heard of already degrades
safely — it renders the generic notification and opens the list — so steps 6
and 7 can even lag step 5 without anything breaking.

The privacy rule from §4 applies unchanged and is the one decision the next
phase must actually make rather than copy: a challenge is *from somebody*,
so the push body must name no player. "You have a new challenge" is the
shape, and the opponent's name is one tap away behind the session.

### 15.16 Migration

`e26a0159c56b` creates both tables, and A64-021.6A adds **no migration at
all** — widening the push type set is a code change over tables that already
exist. **No backfill** — there are no
subscriptions on the day it runs, so nothing fans out and no notification
already stored generates a push.

---

## 16. Production contract — A64-021.7

The audit's output. Everything below is a statement about the system **as
built**, verified rather than intended, and this section is the one to read
before deploying or extending the epic.

### 16.1 Channel matrix

| | `IN_APP` | `EMAIL` | `PUSH` |
| --- | --- | --- | --- |
| Available when | always | `RESEND_API_KEY` **and** `NOTIFICATION_EMAIL_ENABLED` | a valid VAPID pair |
| Types | all six | 3 tournament | 3 tournament + 2 social |
| Preference read at | **write** time (no row is created for a muted category) | delivery time | delivery time |
| Durable store | `notification` | `notification_email_delivery`, PK `notification_id` | `notification_push_delivery`, PK `(notification_id, subscription_id)` |
| Worker | none — the write *is* the delivery | `NotificationEmailDeliveryTask` | `NotificationPushDeliveryTask` |
| Retry | n/a | 5 attempts, 60s→6h exponential | identical |
| Permanent failure | n/a | row `failed`, notification untouched | row `failed`; `410` also revokes the subscription |
| Metric | `gateway.notification_pushes_total` (realtime hand-off) | `notifications.email.deliveries{type,outcome}` | `notifications.push.deliveries{type,outcome}` |
| Operator | — | `notification_email status` | `push_keys status` |

**Three deliberate asymmetries**, each a consequence of the transport rather
than an oversight:

*In-app reads the preference at write time* and the other two at delivery
time. A muted in-app category should leave no row at all — the list is the
record, and a hidden row is a record nobody can see. Email and push are
*deliveries* of a record that already exists, so a person who mutes between
the event and the send must not be sent to.

*Push is keyed per device* and email per notification. One address, many
browsers.

*Only push revokes on failure.* A `410` means the browser is gone, which has
no email equivalent — a bounced address may be temporary, and this platform
does not delete an address on one bounce.

### 16.2 Type matrix

| Type | Category | in-app | email | push | Target | Source event |
| --- | --- | --- | --- | --- | --- | --- |
| `friend_request_received` | social | ✓ | — | ✓ | `friend_requests` | `friends.friend_request_sent` |
| `friend_request_accepted` | social | ✓ | — | ✓ | `player_profile` | `friends.friend_request_accepted` |
| `tournament_registration_confirmed` | tournament | ✓ | ✓ | ✓ | `tournament` | `tournament.player_registered` |
| `tournament_round_published` | tournament | ✓ | ✓ | ✓ | `tournament` | `tournament.round_published` |
| `tournament_completed` | tournament | ✓ | ✓ | ✓ | `tournament` | `tournament.completed` |
| `game_completed` | game | ✓ | — | — | `match_replay` | `game.match_completed` |

Exactly-once identity for every type is
`(recipient_id, source_event_id, type)`, a unique constraint rather than a
check.

`game_completed` is in-app only on purpose: the player is looking at the
result screen, so both other channels would notify them of what they are
reading. The social types are absent from email for a different reason — a
friend request is not worth an inbox, and the push and in-app channels reach
somebody faster.

### 16.3 Delivery ordering, and what a provider failure cannot do

    source action  ─┐
                    ├─ one transaction ─→ outbox event
                    ┘
    relay ─→ durable notification ─┐
                                    ├─ one transaction
             email + push debt  ────┘
                                       ↓ (after commit)
                            realtime frame, then the workers

Two properties follow, and both were verified rather than assumed:

**No external call happens inside a transaction.** The workers claim in one
transaction, call the provider outside any, and record in a third.

**A provider failure rolls nothing back.** The source action, the durable
notification and the realtime frame are all committed before a provider is
contacted. A dead Resend or a dead push service costs a delivery row, and
`test_notification_email.py::test_a_dead_provider_leaves_the_in_app_notification_untouched`
is the assertion.

### 16.4 Terminal states

No row can sit `pending` forever. Every retryable outcome is capped, and the
cap is applied at the **last gate before the write** in both channels —
`_resolve`, not the branch that produced the outcome. A64-021.7 moved push's
to match email's: both were correct, only one was correct by construction,
and the asymmetry meant a future path returning `RETRYABLE_FAILURE` from
elsewhere would have retried forever in one channel and not the other.

| Status | Email outcomes | Push outcomes |
| --- | --- | --- |
| `sent` | `delivered` | `delivered` |
| `skipped` | `skipped_preference`, `skipped_unsupported_type`, `skipped_unverified_email`, `skipped_no_email`, `skipped_channel_unavailable` | `skipped_preference`, `skipped_unsupported_type`, `skipped_no_subscription`, `skipped_channel_unavailable` |
| `failed` | `permanent_failure`, `attempts_exhausted` | `permanent_failure`, `attempts_exhausted`, `subscription_gone` |
| `pending` | never terminal — carries a `next_attempt_at` | same |

`next_attempt_at` is `NULL` on every terminal row, so the partial index the
claim reads holds exactly what is still owed. An operator distinguishes
"still trying" from "gave up" by the status alone.

### 16.5 Operator tooling

    python -m app.operator.notification_email status      config + queue
    python -m app.operator.notification_email smoke --to  one real email
    python -m app.operator.push_keys status               config + queue
    python -m app.operator.push_keys generate             a VAPID pair
    python -m app.operator.rate_limits show               effective policy
    python -m app.operator.rate_limits clear              buckets
    python -m app.operator.accounts verify --email        confirm an address

A64-021.7 added the queue half of `push_keys status`. Before it, push had no
operator visibility into deliveries at all — `build_push_delivery_reader`
existed for exactly this and nothing called it, so an operator could see that
a key pair was configured and could not see whether a single push had ever
been delivered, retried or abandoned.

**Every one of these returns aggregates.** The delivery readers are typed as
ports whose only method is `counts_by_status() -> Mapping[str, int]`, so they
cannot name a recipient, an address or an endpoint —
`tests/unit/test_operator_disclosure.py` asserts the surface rather than
grepping the output, because a command that *fetched* a recipient and chose
not to print it would pass the latter.

### 16.6 What the audit found

| # | Finding | Severity | Resolution |
| --- | --- | --- | --- |
| 1 | Push applied its attempt cap inside `_send` rather than at the last gate, unlike email | Latent — correct today, would retry forever if any other path produced a retryable outcome | Fixed centrally in `_resolve`; regression test added |
| 2 | No operator visibility into push deliveries; `build_push_delivery_reader` was dead code | Production readiness — push failures were undiagnosable | `push_keys status` now prints the queue |
| 3 | Push terminal-state behaviour had no test, where email's did | Coverage | `test_retries_stop_at_the_limit` added |
| 4 | Notifications *created* has no metric, only a log line | Observability gap, **not fixed** | See §16.7 |
| 5 | `/health/ready` does not report channel capability | Documented, **not fixed** | See §16.7 |

Nothing else. Preference enforcement, idempotency, failure isolation,
availability, security, privacy, logging redaction, PWA cache exclusions and
the rate-limit profiles were audited and found correct as built.

### 16.7 Accepted gaps

**Notifications created is a log line, not a metric.** `notification_stored`
carries `written` and `duplicates`, so an operator with a log aggregator can
count them; there is no counter. Adding one means giving
`DurableNotificationWriter` a `MetricsRecorder`, which touches its
constructor, the composition root, the contract app and every test that
builds one — more change than an audit phase should make for an answer that
already exists in a different place. Worth doing in the next phase that
touches the writer for another reason.

**Readiness does not report channel capability.** `/health/ready` answers
PostgreSQL and Redis, which are the dependencies that make the API
*unhealthy*. Email and push are optional channels: a tier without them is
correctly configured, not degraded, and reporting them there would invite a
probe that fails a deploy over a channel nobody enabled. The operator
commands answer the question instead, and they answer it better — with the
queue beside the configuration. Revisit only if a dashboard needs it.

**Retention is append-only.** See §9, unchanged. At Arena64's current scale
this is a row per notification per recipient forever: a 128-player tournament
produces roughly 128 registration + 128×rounds + 128 completion rows, so a
weekly tournament is on the order of 10⁴ rows a year. That is not a problem
this year and is a problem eventually. The cleanup this wants is a scheduled
delete of read notifications older than a retention window, using the
`PeriodicTaskScheduler` both delivery workers already use — deliberately
**not** built here, because a deletion policy with no product decision behind
it is a data-loss feature.

### 16.8 Manual smoke checklist

Automated tests cover everything except the two things that need a real
provider and a real device. Run these by hand before a release that touches
notifications.

**Email**

    python -m app.operator.notification_email status     → transport: resend
    python -m app.operator.notification_email smoke --to you@example.com

Expect a provider message id printed, the mail to arrive, and the sender to
read `Arena64 <no-reply@arena64.gg>`. Nothing automated sends real email.

**Push** — needs a production build; the dev server registers no service
worker.

    apps/api: python -m app.operator.push_keys status    → push: configured
    apps/web: npm run build && npm run preview           → http://localhost:4173

1. Fresh profile → sign in → `/settings/notifications`
2. "Enable push notifications" → browser prompt → allow
3. State becomes "receives friend and tournament push notifications"
4. Background the tab; from a second account, send a friend request
5. Push arrives within one worker interval (~30s)
6. Click it → `/friends/requests` opens
7. Accept → the original sender receives an accepted push
8. Turn push off → `push_keys status` device count drops
9. Sign out, sign in as somebody else → the first account receives nothing

**In-app**

Trigger any notification; the bell badge updates without a refresh, the row
opens its target, and marking it read is idempotent.

**OTP**

Register → the code arrives → verify → a product write (a friend request)
becomes available.

### 16.9 Readiness for Friend Challenges

The seam is §15.15 and the audit did not change it. What A64-021.6A
demonstrated is the cost: adding two pushable types to a live channel was
**two enum members and one line of documentation**, with no migration, no
new table, no worker change and no payload change.

A challenge is the same shape. The only real work is the `challenges` module
and its two events; everything downstream already exists and is audited.

---

## Related documents

- `docs/01-architecture/domain-model.md` §9.3 — the `Notification` aggregate and NT-1…NT-4
- `docs/01-architecture/database.md` §10.2 — the notification relation
- `docs/01-architecture/database.md` §10.3 — the preference relation
- `docs/07-decisions/ADR-003-pwa-service-worker.md` — the worker a push channel will extend
- `specs/frontend.md` §21 — the in-app read surface
- `specs/frontend.md` §22 — the preference screen
- `apps/api/.env.example` — the email and push settings an operator must supply
- `specs/frontend.md` §23 — the push settings screen and the permission flow
