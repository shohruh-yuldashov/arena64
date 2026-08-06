# Notifications

> **Status:** foundation implemented — A64-021.1; realtime in-app delivery — A64-021.2
> **Owner:** platform
> **Related:** `docs/01-architecture/domain-model.md` §9.3, `docs/01-architecture/database.md` §10.2, `specs/friends.md`, `specs/frontend.md` §21

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

Two, and each has a source event that names its recipient unambiguously.

| Type | Category | Source event | Recipient | Target |
| --- | --- | --- | --- | --- |
| `friend_request_received` | `social` | `friends.friend_request_sent` | the addressee | `friend_requests` |
| `friend_request_accepted` | `social` | `friends.friend_request_accepted` | the requester | `player_profile` |

**Neither actor is told what they just did.** Sending a request notifies the
addressee; accepting one notifies the requester. A notification about your
own action is not a notification.

`friends.friend_request_sent` is **new in A64-021.1**. The fact has existed
since A64-013.2 and reached only a log line, so nobody was ever told they
had a request waiting. Publishing it changes nothing about the request
itself.

### 2.1 Categories

`social`, `game`, `tournament`, `system`. Only `social` has a producer
today; the other three exist because the *category* is the unit a future
preference will key on (`database.md` §4.9), and adding one later means
migrating rows written without it.

`marketing` is deliberately absent: this product defines no such
notification, and a category nothing produces is a preference that silently
does nothing.

### 2.2 Deferred producers, and why each is deferred

| Candidate | Why not yet |
| --- | --- |
| `tournament_registration_confirmed` | No source event. `tournament` publishes nothing when a player enters |
| `tournament_round_published` | `tournament.round_published` carries `(tournament_id, round_number)` and **names no recipients**. Mapping it needs a bracket read and a decision about who a round concerns |
| `tournament_completed` | Same: a `winner_id` and a count, not a participant list |
| `match_found` | `game.match_created` has every fact needed — but a match offer expires in seconds, and a durable row for it would be a list full of dead offers. It belongs with realtime delivery, not with history |
| `friend_online` / `friend_offline` | Genuinely transient. A row per transition would be thousands a day in a list whose value is that it is short |

The last two are the useful distinction: **not every notification is
durable.** `notifications.application.services.durable_notification_writer`
holds the mapping, and a kind absent from it is delivered to the transient
sinks and stored nowhere.

---

## 3. Payload

Each type has a typed payload, decoded against the row's own `type` on the
way out. A row whose JSON does not match raises rather than reaching a
client half-rendered.

Both types today carry an **actor summary**:

| Field | Note |
| --- | --- |
| `actor_player_id` | |
| `actor_username` | |
| `actor_display_name` | Nullable |
| `actor_avatar_object_key`, `actor_avatar_version` | **Not a URL** — the URL is composed at the presentation boundary by `AvatarLinkBuilder`, so no CDN hostname is frozen into a historical row |

**Never stored:** an email address, a private profile field, a token or
ticket, a Redis key, an internal identifier with no public meaning, a stack
trace, or the raw source event payload.

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
| `player_profile` | the actor's username | `/players/{username}` |

The client maps a target onto a route it already owns and renders anything
it does not recognise as a **non-navigable** notification. External
navigation is not merely forbidden in v0.x — no branch of the mapper can
produce a scheme.

A received request targets the list where it can be *answered* rather than
the sender's profile; an acceptance targets the new friend, because there is
nothing left to answer.

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

## 10. Preferences — the gap, stated exactly

`database.md` §4.9 specifies `users.notification_preference` keyed by
`(player_id, category, channel)`. **It does not exist**: no table, no enum,
no endpoint. `PATCH /profile/preferences` knows `gameplay` and `locale` and
nothing else.

So A64-021.1 adds **no preference switch**, because a switch the backend
cannot enforce is worse than none — it tells a player they have muted
something they have not.

What is prepared: every notification carries a `category`, which is the
column a preference query would filter on, and it is denormalised onto the
row rather than derived, so that filter can use an index.

NT-4 — *preferences are read at delivery time, not at creation time* — is
the rule whoever builds this must honour, and it is why the check belongs in
a delivery channel rather than in the durable writer.

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

A64-021.2's row is gone from this table because it was built: the seam it
used — a `NotificationAnnouncer` port satisfied at the composition root — is
the same one each remaining row names, and §11 is what it looks like when
taken.

| Deferred to | What it adds | Where |
| --- | --- | --- |
| **A64-021.3 browser push** | Permission request, `pushManager.subscribe`, a VAPID key as a `VITE_` variable, `push` and `notificationclick` handlers in **the existing** service worker, a device/subscription table | `apps/web/pwa/service-worker.ts` and `apps/api`. `shared/pwa/push-support.ts` already reports capability and asks for nothing |
| **Email** | A provider, templates, a per-channel delivery record (`notification_delivery`, `database.md` §10.2) | A third sink, plus the delivery table NT-1 needs to record a channel failing independently |
| **Friend challenges** | A `challenges` domain, its events, and one notification type per event | A new member of `NotificationType`, its payload, its target |
| **Tournament notifications** | A recipient mapping for `round_published` and `completed` — a bracket read, not a payload change | A consumer that resolves participants, feeding the same durable writer |

The worker's message contract must stay as narrow as it is: adding a `push`
handler must not widen what a *page* may tell the service worker to do.

---

## 13. Security

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
| A source event cannot notify an unrelated recipient | The recipient is derived from the event's own participants, re-read at delivery |

---

## Related documents

- `docs/01-architecture/domain-model.md` §9.3 — the `Notification` aggregate and NT-1…NT-4
- `docs/01-architecture/database.md` §10.2 — the relation
- `docs/07-decisions/ADR-003-pwa-service-worker.md` — the worker a push channel will extend
- `specs/frontend.md` §21 — the in-app read surface
