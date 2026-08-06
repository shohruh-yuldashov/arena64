# Realtime / WebSocket Architecture

> **Status:** Approved. The Live Game epic is complete — the authenticated connection
> foundation (A64-016.1), game room sessions (A64-016.2), live move submission (A64-016.3),
> the durable move log with terminal settlement (A64-016.4), distributed clocks (A64-016.5),
> state synchronisation and reconnection (A64-016.6), spectating (A64-016.7) and the closing
> audit (A64-016.8). Readiness and every accepted limitation are in
> [`specs/live-game/audit.md`](../../specs/live-game/audit.md).
> **Owner:** _Unassigned_
> **Last reviewed:** 2026-08-04 (A64-016.8 — Live Game audit)
> **Implements:** architecture.md AD-09 (single-use tickets), AD-11 (one multiplexed socket),
> R-7 (the gateway contains no domain logic)
> **Code:** `apps/api/app/gateway/`

## Purpose

How a client holds a persistent, authenticated connection to Arena64, and what it may say on
one. A64-016.1 built the connection; everything a connection *carries* arrives later.

---

## 1. Scope

| In this document | State |
| --- | --- |
| Ticket-based authentication and the handshake | **Built** — A64-016.1 |
| Connection lifecycle and cleanup | **Built** |
| Multi-connection presence semantics | **Built** |
| Heartbeat protocol | **Built** |
| Message envelope and message types | **Built** |
| Channel multiplexing (AD-11) | **Built** — A64-016.2, §13 |
| Node identity and cross-node route resolution | **Built** — A64-016.2, §5, §14 |
| Game room sessions and membership | **Built** — A64-016.2, §15 |
| Move submission, validation and fan-out | **Built** — A64-016.3, §17 |
| Per-connection move rate limiting | **Built** — A64-016.3, §17.6 |
| Durable move log (AD-18's second half) | **Built** — A64-016.4, §18 |
| Terminal detection and match settlement | **Built** — A64-016.4, §18.4 |
| Remote transport bus seam | **Built** — A64-016.4, §18.6. Single-node adapter only |
| Clocks, spectators, chat | **Deferred** — §9, and A64-016.5 for clocks |
| Reconnection replay (AD-12) | **Deferred** — §9 |

---

## 2. Why the gateway is a tier, not a module

architecture.md names it twice and both times as transport: the module diagram labels it
`gateway — transport only`, and R-7 states "the gateway contains no domain logic. It validates,
authenticates, rate-limits, routes, and fans out. It never decides whether a move is legal."

So the code lives at `app/gateway/`, beside `app/api/` rather than under `app/modules/`. A
bounded context would come with a `domain/` package and the first thing anyone would do is put
something in it — and there is nothing to put. A connection is not an aggregate, a heartbeat is
not a business rule, and the one decision the tier makes (when a player becomes online) is
`users`' rule invoked through `users`' published port.

**Enforced, not asserted.** `.importlinter`'s `gateway-reaches-modules-through-public` contract
forbids `app.gateway` from importing any module's `domain`, `application` or `infrastructure`.
It reaches exactly three things: `users.public`'s `PresenceRecorder`,
`auth.presentation.dependencies`' ticket service, and its own Redis registry.

---

## 3. Authentication handshake — AD-09

### 3.1 The flow

```
  client                    HTTP API                  gateway              Redis
    │                          │                         │                   │
    │  POST /auth/ws-ticket    │                         │                   │
    │  Authorization: Bearer   │                         │                   │
    ├─────────────────────────►│                         │                   │
    │                          │  SET wsticket:v1:<digest> EX 30             │
    │                          ├────────────────────────────────────────────►│
    │  201 { ticket, expires_at }                        │                   │
    │◄─────────────────────────┤                         │                   │
    │                                                    │                   │
    │  GET /ws?ticket=<value>            (upgrade)       │                   │
    ├───────────────────────────────────────────────────►│                   │
    │                                                    │  GETDEL <digest>  │
    │                                                    ├──────────────────►│
    │                                                    │  ZADD gwconn:v1:  │
    │                                                    ├──────────────────►│
    │                                                    │  SET presence:v1: │
    │                                                    ├──────────────────►│
    │  { "v":1, "type":"connection.ready", ... }         │                   │
    │◄───────────────────────────────────────────────────┤                   │
```

### 3.2 Why a ticket and not an access token

AD-09's reasoning, restated because it is the premise everything else rests on: **browsers
cannot set custom headers on a WebSocket handshake.** That leaves two bad options — a long-lived
token in the query string, which lands in load-balancer logs, proxy logs and browser history, or
an unauthenticated socket that authenticates in its first frame, which means holding and
accounting for unauthenticated connections and is trivially floodable.

A ticket valid for seconds and redeemable once makes log leakage worthless: by the time anyone
reads the line, the value has both expired and been spent.

### 3.3 What the ticket is

| Property | Value | Why |
| --- | --- | --- |
| Shape | 256 bits from a CSPRNG, SHA-256 hashed, stored under its digest | DB-24 — the same mechanism as refresh, verification and reset tokens |
| **Not** a JWT | — | A signed token cannot be single-use: verification is a pure function of the token and the key, so the second presentation verifies as well as the first. Making one single-use needs a server-side record, at which point the signature does no work the record is not already doing |
| Lifetime | `GATEWAY_TICKET_TTL_SECONDS`, default 30 | One round trip plus a TLS handshake on a bad mobile connection |
| Redemption | `GETDEL` — **one command** | `GET` then `DEL` lets two nodes both read before either deletes, and both then hold a valid ticket for the same player. That is not theoretical: a second tab opening while the first connects produces exactly this traffic |
| Binds | A player, and optionally the `auth` session | Nothing more. A ticket carrying a match or a permission would be an authorization decision made thirty seconds before it is used |
| Issued by | `POST /api/v1/auth/ws-ticket`, behind `CurrentUser` | **Not a second authentication mechanism.** The ticket is downstream of an ordinary `TokenValidator` check, so exactly one thing on the platform decides whether a credential is valid |

### 3.4 The socket is accepted before the ticket is checked

Backwards at first glance, and required by the protocol: a close frame carrying a code the
client can read only exists *after* the upgrade. Refusing before it produces an HTTP error that
a browser's `WebSocket` surfaces as an untyped `error` event with no code and no reason.

The cost is bounded — one accept, one frame, one close with `1008`, with nothing registered and
no presence written. AD-09's concern is that the gateway must not "hold and account for
unauthenticated connections"; this holds one for the duration of a single `GETDEL`.

A handshake with **no** `ticket` parameter at all is refused by FastAPI before the handler runs,
which is the cheapest possible answer to the scanner traffic that is most of what an
internet-facing `/ws` receives.

### 3.5 What a refusal tells the client

| Situation | `error` code | Close code | What the client should do |
| --- | --- | --- | --- |
| Ticket unknown, expired **or** already spent | `invalid_ticket` | `1008` | Mint a new ticket. The three are deliberately indistinguishable — telling them apart is a step-by-step oracle for the ticket format |
| Ticket was valid, the registry could not be written | `internal_error` | `1011` | Retry with a **fresh** ticket. The one just presented has been spent regardless |

---

## 4. Connection lifecycle

```
  redeem  ──►  register  ──►  connection.ready  ──►  [ ping/pong ]*  ──►  unregister
                   │                                                          ▲
                   └──────────────── every path below ────────────────────────┘
```

Cleanup runs for **all five** endings: a normal disconnect, a peer that goes away mid-write, a
malformed-message failure, a server-side exception, and a heartbeat timeout. It is a `try/finally`
rather than five branches, because five branches is five places to forget one.

**The register is the first statement inside the `try`, deliberately.** A connection that was
never registered must not be unregistered: `unregister` on an absent entry returns the player's
*true remaining count*, which would mark them offline while another tab is open.

Unregistering happens **exactly once** per registered connection, and that is structural rather
than flag-guarded: one caller, one `finally`, over a block entered once.

---

## 5. Connection registry — `gwconn:v2:`

### 5.1 The keyspace

```
gwconn:v2:<player_id>  ->  sorted set
                           member = "<connection_id>|<node_id>"
                           score  = expiry, epoch seconds
```

| Property | Value |
| --- | --- |
| **Owner** | The gateway (`app/gateway/registry.py`). Sole writer — caching.md C-8 |
| **Instance** | `cache`. Derived, expendable, reconstructible by a reconnect |
| **TTL** | `GATEWAY_CONNECTION_TTL_SECONDS` per member, by score; `EXPIRE` on the key at that plus a 60-second margin |
| **Invalidation** | None explicit. A member is removed by `unregister` on disconnect, rescored by the heartbeat, and otherwise falls out of every read once its score passes. The key-level `EXPIRE` exists because Redis deletes a sorted set only when its last member is *removed*, and nothing removes one that merely expired by score |
| **Growth** | One key per player with a live connection; one member per connection. A member is roughly 60 bytes (two identifiers and a separator), so 40,000 concurrent sockets is a few megabytes. Bounded by concurrency, never by history |
| **Failure posture** | **Propagates.** A connection that cannot be registered is one nothing can route to — the exception to caching.md C-7, alongside `wsticket:v1:` |

### 5.2 What v2 changed

v1 stored the connection id alone. That answered "does this player have another connection",
which is all presence needed, and it could not answer **which process holds the socket** — so
nothing could route a message to it. A64-016.1's own known-gaps list named this as the shape
change required before cross-node delivery.

The node is packed into the **member** rather than into a second structure. The alternatives, and
why each is worse:

| Alternative | Why not |
| --- | --- |
| A hash `gwconn:v2:routes:<player>` beside the sorted set | Two structures expiring on two different mechanisms. A member reaped by score leaves a route the hash still reports — and the thing that drifts is the thing that routes |
| A key per connection, `gwconn:v2:conn:<connection_id>` | The fleet's key count becomes its connection count, and "how many connections does this player have" becomes a `SCAN` |

One sorted set keeps every property v1 argued for — self-healing by score, one atomic transaction
per operation, counts returned from the writes — and adds the location for the cost of parsing
one separator.

### 5.3 Migration from v1

**None is performed and none is needed.** The two prefixes are disjoint, nothing writes v1 after
this deploy, and a v1 key holds at most `GATEWAY_CONNECTION_TTL_SECONDS` plus its margin of
state — ninety seconds and change — before Redis drops it.

During a rolling deploy an old node reads and writes v1 while a new one reads and writes v2, so
presence is computed **per generation** for the length of the rollout: a player with a tab on
each sees themselves online from both, and the worst case is one redundant `is_online=True`
write. That degradation is what C-2's version segment exists to make possible, and it is why the
segment is there rather than the value being widened in place.

No backfill, no dual-write, no cutover flag. A keyspace whose entire content expires in ninety
seconds does not need any of them.

### 5.4 Why a sorted set and not a counter

`INCR` on connect and `DECR` on disconnect is smaller, faster, and wrong in the one way that
matters: **it cannot be repaired.** A gateway node killed mid-deploy never runs its decrements,
so the counter is permanently too high and its players are online forever — and nothing can tell
a leaked increment from a real connection.

Scoring each connection by its expiry makes the structure self-healing: a dead node's entries
fall out of every count on the next operation by *any* node, without coordination and without a
sweeper. Same argument `presence:v1:` makes for a TTL over a swept row, applied to a value that
holds more than one thing.

### 5.5 Why the counts come back from the writes

`register` returns the live count including the new connection; `unregister` returns what
remains. Both from a single `MULTI`/`EXEC`.

That is what makes multi-tab presence correct across a fleet. The alternative — write, then read
the count — has a window that another node's connect or disconnect lands in, so two closing
sockets can both read zero and take a connected player offline. Reading the count *from* the
write removes the window: exactly one caller ever sees `1` on the way up and exactly one ever
sees `0` on the way down.

Proven against real Redis in `tests/contract/test_gateway_redis.py`, not against a fake.

## 6. Presence — multi-connection semantics

| Event | Condition | Action |
| --- | --- | --- |
| Connection registered | `register` returned `1` | `record_presence(is_online=True)` |
| `ping` received | always | Refresh the registry entry **and** re-record presence |
| Connection unregistered | `unregister` returned `0` | `record_presence(is_online=False)` |
| Connection unregistered | `unregister` returned `> 0` | **Nothing.** Another tab is open |
| Unregister failed | — | **Nothing.** Marking offline would be a guess; the TTL settles it |

A player may hold as many connections as they have tabs and devices. Presence goes offline only
when the **final** one closes.

**The heartbeat refreshes two windows, and both are needed.** The registry entry expires on
`GATEWAY_CONNECTION_TTL_SECONDS` and the presence record on `PRESENCE_TTL_SECONDS`; they have
different owners and different values. Refreshing one and not the other produces either a player
reported offline while holding a socket, or a socket the fleet has forgotten while the player
still shows online.

The gateway **records** presence and applies no privacy. Who may see it is decided by profile
composition (`profiles`), which holds the read port alone — the gateway holds the write port
alone, and neither can do the other's job.

`device_type` is recorded as `web` unconditionally, which is honest rather than lazy: nothing on
the handshake carries a device claim, and a value inferred from a `User-Agent` string would be a
guess written into a keyspace where a field added later decodes short on every key written
before it.

---

## 7. Heartbeat

```
client ──►  { "v": 1, "type": "ping", "request_id": "beat-17" }
server ──►  { "v": 1, "type": "pong", "payload": {}, "request_id": "beat-17" }
```

**The client drives it; the server enforces a deadline.** A server that pinged would need a
timer per socket — 40,000 timers on a gateway node, each waking to discover nothing changed.
Instead the server's read is `wait_for(receive(), GATEWAY_HEARTBEAT_TIMEOUT_SECONDS)`: an idle
connection costs one pending future, and the timeout is the enforcement.

| Setting | Default | Meaning |
| --- | --- | --- |
| `GATEWAY_HEARTBEAT_TIMEOUT_SECONDS` | 45 | Silence after which the server closes the connection |
| `GATEWAY_CONNECTION_TTL_SECONDS` | 90 | How long the registry believes in a connection without a refresh |
| `PRESENCE_TTL_SECONDS` | 60 | How long the platform asserts a player is online |

**Clients should ping at roughly a third of the heartbeat timeout** (≈15s), which leaves room
for two lost frames before a healthy connection is dropped.

The three nest, and the ordering is validated at startup rather than documented and hoped for: a
misordering produces a *working* gateway with a subtly wrong liveness model, and the symptom
appears under load, days later, on somebody else's dashboard.

---

## 8. Message envelope

### 8.1 Shape

```json
{ "v": 1, "type": "pong", "channel": "system", "payload": {}, "request_id": "beat-17" }
```

| Field | Required | Notes |
| --- | --- | --- |
| `v` | yes | `PROTOCOL_VERSION`. A frame with any other value is refused — AD-11 multiplexes one socket, so a client has to be able to say what it speaks before the server sends something it cannot parse |
| `type` | yes | A member of the closed set in §8.2. An open string would be a router dispatching on whatever a client sends |
| `channel` | no | Which logical stream (§13). **Absent means `system`**, which is what makes this a backwards-compatible addition rather than a protocol bump — an A64-016.1 client sends none and every frame it sends is a system frame. An *unknown* channel is refused rather than defaulted |
| `payload` | no | An object. Defaults to `{}` |
| `request_id` | no | Echoed back on the response. A non-string or one over 64 characters is **dropped rather than rejected** — the field is a courtesy, and echoing an unbounded client-supplied value is an amplification primitive |

`request_id` is carried and echoed **now**, before anything needs it, because the round trip that
first needs it (A64-016.2's move submission, and AD-23's optimistic board matching a confirmation
to the move it confirms) would otherwise have to change the envelope for every existing type at
once.

### 8.2 The message types

| Type | Channel | Direction | Meaning |
| --- | --- | --- | --- |
| `connection.ready` | `system` | server → client | Sent once, immediately after redemption. The signal that the socket is **authenticated**, not merely open |
| `ping` | `system` | client → server | Heartbeat |
| `pong` | `system` | server → client | Heartbeat answer, correlated |
| `room.join` | `game` | client → server | Enter one match's routing scope. Carries `match_id` and **nothing else** — §15.2 |
| `room.leave` | `game` | client → server | Leave a room. Idempotent |
| `room.joined` | `game` | server → client | Confirmation, with the participants and whether both are connected |
| `room.left` | `game` | server → client | Confirmation. Sent for an idempotent leave too |
| `game.move.submit` | `game` | client → server | One move — §17.1 |
| `game.move.accepted` | `game` | server → client | The submitter's acknowledgement, correlated by `request_id` |
| `game.move.rejected` | `game` | server → client | The submitter's refusal, with a stable category |
| `game.move.applied` | `game` | server → client | The broadcast **both** participants receive. No `request_id` |
| `notification.created` | `notifications` | server → client | A durable notification now exists for **this recipient** — A64-021.2. Three fields, and no rendered text: `notification_id`, `type`, `created_at`. The frame says something happened; `GET /notifications` says what |
| `error` | the failing frame's | server → client | A refusal about the *frame*, carrying a code |

**This table names the frames the connection carried when each phase added them, and
`app.gateway.protocol.MessageType` is the source of truth** — the game, draw, spectator and
matchmaking frames added between A64-016.3 and A64-021.2 are enumerated there with a docstring
each. What has never changed is the rule the original twelve established: every type is
implemented when it is added. A type without the handler behind it is the speculative generality
CLAUDE.md §1.7 forbids, and the platform has declined it three times — `TokenType.ACCESS` alone,
`PasswordHasher.hash` alone, and A64-016.1's four-member `MessageType`.

The `notifications` channel arrived with `notification.created` and is an **addition, not a
version bump**: a client that does not know a channel treats it as `system`, and one that does not
know a type ignores the frame, so `PROTOCOL_VERSION` stays at 1.

### 8.2a The dispatch table — A64-016.3 §1

A64-016.1 shipped two branches, A64-016.2 four, and both said the same thing: a table earns its
place when a handler needs its own collaborators. `MoveSubmissionHandler` holds six, so the
branching became a `dict` from `MessageType` to a bound handler.

| Property | How |
| --- | --- |
| Deterministic | A dict literal over a closed enum. The same frame reaches the same handler on every process, every time |
| No dynamic imports | Nothing imports at call time |
| No reflection | No decorator registry, no metaclass, no module scanning |
| Handlers get only what they need | `ping` takes the connection; the move handler takes its own collaborators |
| Complete | `CLIENT_SENDABLE` names the four types a client may send; a test asserts the table covers exactly those |

**Not a generic event framework**, which §1 forbids: no middleware chain, no priorities, no
wildcard subscriptions, no handler that can register another. Adding a message type is one entry
and one method.

The decode and the send stay outside it. Decoding fails before there is a type to dispatch on,
and sending is the read loop's — so one place owns the socket, which is what makes AD-11's
cross-stream ordering a property of the loop rather than of every handler behaving.

### 8.3 Errors carry a code, never prose

| Code | Meaning |
| --- | --- |
| `invalid_ticket` | Nothing redeemable was presented. The connection closes immediately after |
| `malformed_message` | Not JSON, not an object, too large, unknown type, unknown channel, or the wrong protocol version. **The connection stays open** |
| `not_a_participant` | A `room.join` for a match this player is not in — **or one that does not exist.** One code for both: distinguishing them makes live match identifiers enumerable by response |
| `room_unavailable` | A participant, but the match is not in a state that has a room (§15.3). Distinct because it discloses nothing the caller does not already know, and the client's response differs — wait, rather than stop asking |
| `internal_error` | Something the client cannot act on beyond retrying |

Free text would be either useless to a client (which cannot branch on prose) or a disclosure
oracle. The server knows precisely what happened and says so in its logs.

**A malformed frame does not close the connection.** A client that sends one bad frame is far
more likely to be a version skew or a bug than an attack, and closing would turn a recoverable
client defect into a fleet-wide reconnect loop. `GATEWAY_MAX_FRAME_BYTES` (8 KiB) is what bounds
the abuse case, and it is checked *before* parsing.

---

## 9. Deferred — what a connection does not yet carry

None of the below exists. Each is listed with the decision it will have to honour, so the next
task starts from the constraint rather than rediscovering it.

| Deferred | Arrives with | The constraint it inherits |
| --- | --- | --- |
| Move submission and acknowledgement | A64-016.3 | R-7 — the gateway routes; `game` decides legality. The envelope's `request_id` is already the correlation token, and the `game` channel already exists |
| Player/spectator channel split | Later | AD-10 — separate channels with independent policy, so a spectator feed can be delayed without touching player latency |
| Clocks | Later | AD-21 — adjudicated by a worker against Redis, never by an in-process timer on one gateway node |
| Reconnection replay | Later | AD-12 — per-match sequence numbers; the client sends its last-seen value and the gateway replays the gap from a bounded stream |
| Cross-node message **transport** | A64-016.3 | The *decision* is built (§14.3): `ConnectionRouter` already partitions recipients into local and remote. What is missing is delivery — a publisher per remote node, which §9 of A64-016.2 deliberately excluded so the seam could be designed and tested on its own |
| Backpressure and per-channel rate limits | Later | The frame-size bound exists; a rate limit per connection does not |

---

## 10. Observability

Three counters and one observation, through `platform.metrics` — so counters aggregate and the
duration keeps its distribution.

| Metric | Labels | Answers |
| --- | --- | --- |
| `gateway.connections_accepted_total` | — | How many sockets completed the handshake |
| `gateway.connections_rejected_total` | `reason` — `invalid_ticket`, `registration_failed` | Are handshakes failing, and is it us or them |
| `gateway.connections_closed_total` | `reason` — `client`, `heartbeat_timeout`, `server_error` | The first thing to read during an incident: "users are leaving" versus "we are dropping them" |
| `gateway.connection_duration_seconds` | — | An observation. The distribution is bimodal by construction — a tab closed at once versus a game played for an hour — and a mean describes neither population |
| `gateway.room_joins_total` | — | How many sockets attached to a match |
| `gateway.room_join_rejections_total` | `reason` — `not_a_participant`, `room_unavailable` | Are joins failing, and is it the client's problem or ours. **Two members, not three**: a metric that separated "no such match" from "not your match" would let anybody with a dashboard confirm that a match id exists |
| `gateway.room_leaves_total` | `reason` — `client`, `disconnect` | How rooms drain. `disconnect` dominates — most players close the tab rather than leaving politely |
| `gateway.room_states_total` | `state` — `waiting`, `ready` | `rate(…{state=ready})` is "how often does a room complete", which is the question a gauge over active rooms could not answer |
| `gateway.route_resolutions_total` | `locality` — `local`, `remote` | The ratio is the operational question: a rising remote share is what says a cross-node transport is now actually needed |
| `gateway.move_submissions_total` | — | Counted before validation, so `submissions − accepted − rejected` is the number deduplicated by `request_id` |
| `gateway.moves_accepted_total` | — | Moves the game applied |
| `gateway.moves_rejected_total` | `category` — the eight in §17.7 | One counter, not eight: they are mutually exclusive outcomes of one operation, and splitting them would make "what fraction of moves are refused" a sum across metrics |
| `gateway.local_deliveries_total` | — | Frames written to sockets this node holds |
| `gateway.remote_publishes_total` | — | **Per node, not per connection.** A rate that tracks connections is the symptom of a publisher that bypassed the routing plan |
| `gateway.remote_publish_failures_total` | — | Separate from publishes rather than a label on them, because an alert fires on this rate alone |

**No player ids, no ticket ids, no connection ids, no match ids, no request ids and no node ids
in any label.** No board state and no move payload is ever logged — a log of every frame would be
a searchable record of every game. Beyond A64-015.5 §9's
cardinality rule, a ticket id in a label is a credential in a system with broader read access
than the store it came from, and a player id is a record of when somebody was connected — which
is the sleep schedule `show_last_seen` exists to withhold.

**Active connections is not a gauge.** `MetricsRecorder` publishes none: a gauge is read at
scrape time and needs the exporter to call into the process. The count is
`ConnectionRegistry.active_count` — a `ZCARD`, which is a better answer because it is true across
the fleet rather than per process. What is counted here is the *transitions*, from which a count
is derivable and a history is not.

Message payloads are never logged. Frame rejections log a parser detail at `DEBUG` and send the
client a code.

---

## 11. Configuration

| Variable | Default | Notes |
| --- | --- | --- |
| `GATEWAY_TICKET_TTL_SECONDS` | 30 | Floor of 5 — below a plausible round trip this is a ticket that expires before it arrives |
| `GATEWAY_CONNECTION_TTL_SECONDS` | 90 | Must exceed the heartbeat timeout; the process refuses to start otherwise |
| `GATEWAY_HEARTBEAT_TIMEOUT_SECONDS` | 45 | Roughly half the registry TTL |
| `GATEWAY_MAX_FRAME_BYTES` | 8192 | Checked before parsing |
| `GATEWAY_NODE_ID` | *(generated)* | §14.1. Set it in any deployment with more than one replica |
| `GATEWAY_ROOM_TTL_SECONDS` | 3600 | §15.1. Measured against a game, not a heartbeat |
| `GATEWAY_MOVE_RATE_LIMIT_ENABLED` | `true` | §17.6. Off means an unbounded move rate |
| `GATEWAY_MOVE_RATE_LIMIT` | 30 | Per connection |
| `GATEWAY_MOVE_RATE_LIMIT_WINDOW_SECONDS` | 10 | |
| `GATEWAY_MOVE_IDEMPOTENCY_TTL_SECONDS` | 60 | §17.5 |
| `GAME_LIVE_STATE_TTL_SECONDS` | 14400 | A **cache** since A64-016.4 — expiry costs a rebuild from the log, not a game (§18.5) |

There is **no kill switch**. Presence and the friends cache have one because they degrade to a
working platform with a feature missing; a gateway with no registry cannot answer "does this
player have another connection", so presence would flap on every closed tab. A switch whose off
position is "broken" is not a switch — turning the gateway off is a deploy decision.

---

## 12. Known gaps

| Gap | Consequence today | What closes it |
| --- | --- | --- |
| ~~No contract suite over `wsticket:v1:` or `gwconn:v1:`~~ | **Closed by A64-016.2** | — |
| ~~No contract suite over `gwroom:v1:`~~ | **Closed by A64-016.3.** `tests/contract/test_gateway_redis.py` proves atomic membership, per-connection removal, the empty-room TTL and the monotonic sequence CAS against real Redis | — |
| ~~No durable move log~~ | **Closed by A64-016.4** — §18 | — |
| ~~No terminal-state detection~~ | **Closed by A64-016.4** — §18.4 | — |
| `GatewayBus` has no multi-node adapter | A fan-out to another node is queued in memory and never crosses. **Single-node is the only supported topology** | `RedisStreamGatewayBus` — §18.6 |
| No clocks | A game cannot be lost on time; the move log's clock columns are null | A64-016.5 |
| No per-connection limit on non-move frames | `room.join` and `ping` are bounded only by what a socket can send. Both are cheap; neither is free | A second rule if it becomes worth spending |

---

## 13. Channel multiplexing — AD-11

### 13.1 One socket, three streams

AD-11: **one socket per client, multiplexed by channel.** Two reasons, and the second is the one
that matters for a board game — browsers limit concurrent connections per origin and mobile
clients pay a battery cost per socket, but more importantly separate sockets for moves and chat
would make cross-stream ordering undefined, and a resignation and a chat message sent in that
order must arrive in that order.

So the channel is a **field**, not a connection.

| Channel | Carries |
| --- | --- |
| `system` | The connection itself — readiness, heartbeat, transport errors |
| `matchmaking` | Queue and pairing notifications. A64-015.5's pending-match delivery is the first thing that will use it |
| `game` | One live match's traffic — room membership today, moves from A64-016.3 |

**Not per-match.** A channel is a *kind* of traffic; which match a frame concerns is in the
payload. A member per live match would make the enum unbounded, which is exactly what §11 forbids
of a metric label and is no better here.

### 13.2 The channel-bound view

```python
socket.on(Channel.GAME)   # -> a GatewaySocket whose sends are game frames
```

`ChannelSocket` satisfies `GatewaySocket`, which is what §4 means by "another implementation of
the same seam, not a separate service architecture" — it is substitutable everywhere the
lifecycle already passes one. Three things it buys that handing out the raw socket does not:

1. **A handler cannot write on somebody else's stream.** A64-016.3's move handler holds
   `on(Channel.GAME)` and has no way to emit a `system` frame — not because it is told not to,
   but because the object it holds cannot.
2. **The stamp cannot be forgotten.** A frame built without a channel defaults to `system`, which
   is right for the connection's own traffic and exactly wrong for a game frame. Forgetting is
   silent and delivers to the wrong stream.
3. **One writer, so ordering holds.** Every view shares the underlying socket, which is what
   AD-11's cross-stream ordering guarantee requires.

### 13.3 `receive` is deliberately not demultiplexed

A channel view returns **every** frame the peer sent, not only the ones on its channel. That
looks like a gap and is the correct shape for one reader: the connection has a single read loop,
and a per-channel `receive` would mean either several loops competing for one transport — where
a frame delivered to the wrong queue is lost — or a demultiplexer holding per-channel buffers,
which is unbounded memory a slow consumer fills.

The read loop reads once and dispatches on `message.channel`. One place, cannot drop a frame.

---

## 14. Node identity and routing

### 14.1 The node identifier

| Property | How it is held |
| --- | --- |
| Configured at startup | `GATEWAY_NODE_ID`, read once through the cached settings |
| Stable for the process lifetime | Resolved through an `lru_cache`, and carried on `GatewayPolicy` — the lifecycle has no way to mint one, so every connection this process accepts is registered under the same node |
| Never client-facing | No message type carries it and `GatewayMessage` has no field it could land in |
| Never a metric label | One series per node is a cardinality that grows with the fleet (§10) |

At most 32 characters, and it must not contain `|` — the separator between the two halves of a
`gwconn:v2:` member. Both are refused at startup rather than producing routes that decode to the
wrong node.

**With nothing configured** the process draws a random identifier once. That is *correct* for the
registry — the identity that matters there is "this process instance", and a restart genuinely is
a different one — and it is illegible: `d4f1a2b8` says a connection is elsewhere just as well as
`gateway-3` does, but only the second can be found on a dashboard. So the fallback keeps local
development working with no configuration, and a real deployment sets it.

It is deliberately **not** the hostname by default. A process that was not told who it is has no
basis for claiming a name that means something to an operator.

### 14.2 Resolving where a message would go

```
ConnectionRouter.plan_for([player_a, player_b]) -> RoutingPlan
                                                    .local   Sequence[ConnectionRoute]
                                                    .remote  Mapping[node_id, ...]
```

Two fields rather than one list with a flag, because the two halves are consumed by different
code: the local half is a loop over sockets this process holds, and the remote half is a publish
per node. `remote` is **grouped by node** because the eventual transport is one message per node,
not one per connection — a fan-out that published per recipient would send the same frame to one
node as many times as that player has tabs open.

Recipients are deduplicated: a caller fanning out to a room's participants may legitimately name
the same player twice, and a room whose two seats resolved to one account would otherwise deliver
everything twice.

### 14.3 Why the seam exists with no transport behind it

§9 of A64-016.2 asks for "the routing seam required by future tasks" and forbids the broker, the
remote delivery and the Pub/Sub. The split is where the **decision** lives; the transport is
where the plumbing lives. Building both at once means the first thing needing cross-node delivery
has to get two unfamiliar things right in one change.

It is also the honest place for the cost: resolving a plan is one Redis read per recipient, and a
room has two. Having that in the code now means it shows up in a profile before it shows up in an
incident.

Nothing calls `plan_for` yet. That is why there is no half-written publisher beside it.

---

## 15. Game room sessions

### 15.1 What a room is, and is not

A room is **one match's routing scope**: which sockets are attached to a given `match_id`. It
holds no board, no clock, no move history and no result. R-7 is what makes that separation
load-bearing — "the gateway contains no domain logic … it never decides whether a move is legal"
— and a room that knew anything about the contest would be the first place that stopped being
true.

```
gwroom:v1:<match_id>          ->  sorted set, member = "<player_id>|<connection_id>"
                                              score  = expiry, epoch seconds
gwconnroom:v1:<connection_id> ->  set of match ids this connection is in
```

| Property | Value |
| --- | --- |
| **Owner** | The gateway (`app/gateway/room_store.py`). Sole writer |
| **Instance** | `cache` |
| **TTL** | `GATEWAY_ROOM_TTL_SECONDS` (default 3600) per member, by score; `EXPIRE` on both keys |
| **Invalidation** | A member is removed on `room.leave` and on disconnect. The TTL is the **backstop** for a node that died between the two, not the primary mechanism |
| **Growth** | One key per match with someone attached, one member per connection. Bounded by concurrent games, not by history |

**Ephemeral, and not persisted** (§6). AD-19 — nothing competitive lives only in Redis — is
satisfied because a room is derived, at every instant, from two facts that are each durable
elsewhere: the participants come from `game.match`, and the connections come from the registry.
Losing every room costs a reconnect.

The second key is a **reverse index** with exactly one caller: the disconnect path. A socket that
drops never sends `room.leave`, and without it the only cleanup is a `SCAN` over a keyspace
proportional to live matches on every closed tab. It is a genuine drift risk, bounded two ways —
both keys are written in one transaction, and the forward key is authoritative, so a stale entry
costs one wasted `ZREM` and never a phantom member.

### 15.2 Membership rules

| Rule | How it is held |
| --- | --- |
| Only actual match participants may join | `game.public.MatchRosterReader` — a published read with **one method**, so a transport tier that was compromised could enumerate nothing and change nothing |
| The player is never client-supplied | `room.join` carries a `match_id` and has **no field for a player**. The identity comes from the socket's redeemed ticket. Structural, not remembered |
| An unknown match and a non-participant are the same answer | `not_a_participant` for both — the argument `MatchAcceptanceUseCase.accept` makes for collapsing them into `MatchNotFound` |
| A player may hold several connections in one room | A member is the `(player_id, connection_id)` **pair**. A store keyed on the player alone would take every tab out when the first one closed |

### 15.3 Which match states have a room

Today: `active` alone. A match is `active` once both players accepted, which is the first instant
at which "route messages between these two" is a sensible thing to want — before that the
handshake is an HTTP concern and `PendingMatchView` serves it.

The rule lives at the **gateway** (`ROOMABLE_STATES`) rather than in `game`'s reader, because it
is the gateway's policy over `game`'s published state. A later task that wants both players
attached during the handshake changes that predicate and nothing in `game`.

### 15.4 Lifecycle

```
  match is active  ──►  room.join   ──►  member attached  ──►  room.joined
                                                 │
                        room.leave ─────────────►│──► member detached ──► room.left
                        disconnect ─────────────►│
                        nothing for the TTL ────►│──► member lapses, key expires
```

| Property | Rule |
| --- | --- |
| Status | **Derived**, never stored: `ready` when every participant has at least one connection attached, `waiting` otherwise. A stored status is a second copy of what the members already say |
| `ready` ≠ started | Nothing about it activates a match or starts a clock. It says the routing scope is complete |
| Join is idempotent | On `(player_id, connection_id)`. A retry after a dropped response attaches once |
| Leave is idempotent | Leaving a room this connection is not in is not an error and produces `room.left` — the client asked to be out and is out |
| Disconnect detaches | `GameRoomService.detach` runs from the connection's cleanup, **before** the registry unregister, so there is no window in which the fleet has forgotten the socket while a room still reports it attached |
| One tab closing | Removes one member. The player's other connections stay in the room |
| Empty room | Expires. There is no close operation, because a room with no members is indistinguishable from one that never existed |

---

## 17. Move submission — A64-016.3

### 17.1 The frame

```json
{ "v": 1, "type": "game.move.submit", "channel": "game", "request_id": "m17",
  "payload": { "match_id": "…", "path": ["c3", "d4", "f6"] } }
```

Two fields, and the restraint is the design.

**A path, not a from/to pair.** The same origin and destination can be reached by two capture
sequences taking different pieces, and picking one for the player is picking which of their
pieces survives.

**No captures and no promotion.** The client could send them, and the server would then have to
either trust them or verify them — and verifying means generating the legal moves anyway. So the
server asks its own generator for the legal moves in the position and looks for the one whose
path matches: the captures and the crown are the generator's, and a tampered client cannot claim
to have taken a piece it did not jump. Strictly stronger than validation, and less code.

**No player id.** §4 forbids trusting a client-supplied identity, and the frame has no field for
one — the player is the socket's redeemed ticket. Structural rather than remembered.

### 17.2 What the client gets back

| Frame | When | Correlated |
| --- | --- | --- |
| `game.move.accepted` | The move applied | Yes — the submitter's `request_id` |
| `game.move.rejected` | It did not | Yes |
| `game.move.applied` | Broadcast to both participants | **No** |

The submitter receives **two** frames: their acknowledgement and the broadcast. Merging them
would mean a client could not tell its own move from its opponent's without inspecting the
payload, and it would leave two code paths for advancing the board with one rarely exercised.

**Ordering: the submitter may receive `applied` before `accepted`.** The fan-out runs before the
handler returns its answer, and both are idempotent by ply — a client that has already applied
the move optimistically (AD-23) treats the broadcast for its own move as a no-op. Clients must
not assume the acknowledgement arrives first.

Accepted payload: `match_id`, `ply`, `side_to_move`, `fingerprint`, and the server-derived
`applied` move. **A fingerprint, not a board** — the client already applied the move
optimistically and needs confirmation and a divergence check, and a full position on every move
would be the largest payload in the protocol multiplied by every move of every game.

### 17.3 The `game.public` boundary

```
SubmitMoveRequest  ->  SubmitMoveUseCase.submit  ->  SubmitMoveResult
```

One method. The gateway can submit and can do nothing else — it cannot read a position,
enumerate matches or resign one. R-7 ("the gateway never decides whether a move is legal") is
worth nothing if the port it holds is wide.

Checks run in this order, and the order is the point:

| # | Check | Failure |
| --- | --- | --- |
| 1 | The match exists **and** this player is in it | `not_a_participant` |
| 2 | The match is `active` | `match_not_active` |
| 3 | The player owns the side to move | `not_your_turn` |
| 4 | The path is legal here | `illegal_move` |
| 5 | Nobody else wrote first | `stale_state` |

Identity, then state, then rules. A caller who may not see a match learns nothing about it —
step 1 collapses "no such match" and "not yours" — and a caller whose turn it is not is refused
*before* the engine says whether their move would have been legal, which would otherwise be a
free rules oracle against a position they may not reason about.

Before any of that, the **gateway** checks two cheaper things: the rate limit (§17.6) and whether
this connection is in the room (`not_in_room`). Both are one Redis read; the game check is a
database read plus a position load.

### 17.4 Concurrency — compare-and-set on the ply

The live position lives in Redis (AD-18) as `game:live:v1:<match_id>`, a hash of `{ply,
position}` on the **`live`** role. Every write is a Lua compare-and-set conditional on the ply
that was read.

Two moves submitted against the same state produce one application and one `stale_state`,
decided inside Redis. §6 forbids a process-local lock, and a lock would be wrong anyway: the two
players may be on different gateway nodes, so a lock in one process guards nothing.

The read is not locked and does not need to be. A stale read produces a failed CAS, which is
what a lock would have prevented by *waiting* — and waiting is worse, because the loser's move
has almost certainly become illegal by then and the honest answer is to say so.

`expected_ply = 0` also matches an absent key, which is what makes lazy seeding safe: the first
mover computes the opening position from the variant, and two nodes doing so concurrently
resolve to one winner.

**Why `live` and not `cache`.** The one gateway-adjacent keyspace that is not reconstructible by
a reconnect. `cache` is configured to evict, which is correct for presence, the connection
registry and rooms — and would make the eviction policy a way to lose a game.

### 17.5 Idempotency — two mechanisms, two failures

They look interchangeable and are not:

| Mechanism | The failure it handles | Correct outcome |
| --- | --- | --- |
| `request_id` dedupe | The **client retried**. Same connection, same intent, sent twice because the first answer was lost | The *first* answer, replayed |
| Ply compare-and-set | The **opponent moved first**. Two different intents against the same state | One applied, one refused |

A CAS alone would let a retry fail as `stale_state` rather than returning the original success.
A dedupe alone would let two genuinely concurrent moves both apply.

**Scope: `(connection_id, request_id)`**, stated explicitly per §7. Not the player — two tabs
choosing `"1"` are two independent clients, and keying on the player would make one tab's retry
return the other's answer. Not the match — that would do the same across a reconnect, where a
client legitimately restarts its counter.

**Bounded**: `GATEWAY_MOVE_IDEMPOTENCY_TTL_SECONDS`, default 60, in `gwmove:v1:` on `cache`.
§7 forbids "a permanent unbounded request cache". A frame with **no** `request_id` is not
deduplicated at all — there is nothing to key on, and inventing one would be the second
correlation identifier §7 forbids.

What is stored is the **encoded response frame**, including rejections, so a retry replays
exactly what the client already saw rather than a decision re-rendered.

### 17.6 Per-connection move rate limiting

Move submission is the first expensive per-frame operation: a database read, a position load, a
legal-move generation and a Redis CAS.

| Property | Value |
| --- | --- |
| Scope | `RateLimitScope.CONNECTION` — new in A64-016.3 |
| Default | 30 moves per 10 seconds |
| Implementation | The platform's sliding-window limiter, not a second one |
| Placement | **Before the decode and before every read** (§13) |
| On violation | `game.move.rejected` with `rate_limited`. The connection **stays open** |
| `ping` | Not charged. Rate-limiting a heartbeat disconnects the players who are behaving |

Per connection rather than per player: a player with two tabs is two clients, and a shared bucket
would let one tab's misbehaving loop throttle the other's game — which on a live board is losing
to somebody else's bug.

Fails **open**, like every other limit on this platform: a limiter outage that refused moves
would stop every game in progress.

### 17.7 Error mapping

Every failure becomes a stable category and a **fixed sentence**. The sentence never comes from
the exception, which is what makes §14's "no SQL errors, no Redis errors, no Python class names,
no stack traces" a property of a lookup table rather than a rule somebody remembers.

| Code | Meaning | What the client should do |
| --- | --- | --- |
| `not_in_room` | This connection has not joined the match's room | Send `room.join`, then retry |
| `not_a_participant` | Not this player's match — **or** no such match | Stop |
| `not_your_turn` | Out of turn | Resynchronise |
| `illegal_move` | The path is not legal here | Log loudly — under AD-14 the client's own engine disagreed |
| `stale_state` | Another writer advanced the match first | **Retry** |
| `match_not_active` | Still in acceptance, declined, expired, or finished | Stop |
| `rate_limited` | Too many moves from this connection | Slow down |
| `internal_error` | Something the client cannot act on | Retry |

Anything unmapped is `internal_error`, with the exception type and traceback in the log where the
caller cannot read it.

### 17.8 Fan-out over `RoutingPlan`

```
recipients  ->  RoutingPlan  ->  local:  write to sockets this node holds
                             ->  remote: one ForwardingRequest per node
```

§8 forbids bypassing the plan and forbids rediscovering routes inside the publisher; both are
structural. `RoomBroadcaster` asks the router once, and `ForwardingRequest` already names the
connections — the publisher has no registry to consult.

**One publish per node, not per connection.** The plan groups by node, so a fan-out to a player
with four tabs on one node is one request carrying four connection ids. A publisher that iterated
routes would send the same frame four times to the same process, and it looks identical to a
working system until the fleet grows.

`ForwardingRequest` is **primitive-only** — a node id, connection ids and the encoded frame. No
socket, no FastAPI object, no engine type, because the eventual transport serialises it.

**Delivery never fails a move.** By the time it runs the move is committed in `game`, so a socket
that went away between the plan and the write is counted and logged, never raised (§9, §10). The
implementation behind `RemoteNodePublisher` is a **log line** until a multi-node adapter exists — see §18.6.

### 17.9 Room state — the live projection

`gwroomstate:v1:<match_id>` holds `{ply, side_to_move, fingerprint}`. The minimum a client needs
to notice it has fallen behind and a router needs to order what it delivers.

**Not the board.** `game` is authoritative (AD-18); §11 says "do not duplicate the complete Game
aggregate in `gwroom:v1:`", and a board here would be a copy that can disagree with the authority
and is updated by a fan-out that is allowed to fail.

The write is a **monotonic** compare-and-set, not an `HSET`. §12 requires that a concurrent
update cannot silently overwrite a newer sequence, and under at-least-once fan-out an
out-of-order delivery is ordinary rather than rare — a plain write would leave the room reporting
an older ply and send a resynchronising client backwards.

---

## 18. The durable move log and terminal settlement — A64-016.4

AD-18: *"Live position lives in Redis. **Moves are appended durably to
PostgreSQL.**"* A64-016.3 built the first sentence. This is the second, and with it the
mitigation AD-19 depends on — *"the durable move log allows a match to be reconstructed by replay
through the engine"*.

### 18.1 The schema

```
game.move   id, match_id, ply_number, seat, path[], captured[], promoted_to,
            position_hash, engine_version, think_time_ms, remaining_clock_ms, created_at
```

| Constraint | Rule |
| --- | --- |
| `uq_move__ply` | Unique `(match_id, ply_number)`. **The concurrency mechanism**, not a safety net |
| `ck_move__ply_positive` | Contiguous from 1 (MT-5) |
| `ck_move__path_is_a_move` | At least two squares |
| `ck_move__position_hash_present` | Non-**empty**, not merely non-null — an empty hash is a replay that cannot check itself |
| `fk_move__match` | `ON DELETE CASCADE`. A log without its match is unreadable, and retention cannot reach a `completed` match — `ix_match__abandoned` excludes it |

**Append-only** (MT-5): no `updated_at`, no `deleted_at`, and no transition on `MoveRecord`. The
stronger form `database.md` §8.4 asks for is a runtime role without `UPDATE`/`DELETE` on this
table — a grant rather than a migration, and the next step.

`game.match` gains `ply_number`, `outcome`, `termination_reason`, `winner`, `ended_at`, and the
status enum gains `completed`.

### 18.2 What this is, against `database.md` §8.4

§8.4 specifies the relation at scale. This is the subset that ships, documented the way §8.2a
documents `game.match`'s own:

| §8.4 | Here | Why |
| --- | --- | --- |
| Partitioned monthly on `match_created_at` | Not partitioned | `game.match` is not either, and a child partitioned on a parent key that does not exist cannot declare a foreign key. Both move together (DB-13) |
| `path` as `smallint[]` of PDN numbers | `text[]` of algebraic squares | There is no PDN numbering on `BoardCoordinate`. The engine round-trips algebraic (`parse`/`__str__`), and storing a numbering it cannot produce would be the lossy translation §4 forbids |
| `position_hash` as `bytea` | `text` | `MoveRecord.resulting_position_hash` **is** `Position.fingerprint`, a string. Hashing it would add a second representation of one value |
| Clock columns `NOT NULL` | Nullable | A64-016.5 fills them. Null rather than zero: a clock reading of zero is a flagged player |
| `client_move_id` | Absent | `uq_move__ply` is the duplicate guard; the client's retry token is `request_id`, and A64-016.3 §7 forbids a second one |
| `received_at` | Absent | It exists for the flag race (MT-9), which is clocks |

### 18.3 The transaction

One PostgreSQL transaction per move:

```
1. lock the match row          FOR UPDATE, no SKIP LOCKED
2. rebuild the Match           replay the durable log
3. match.play(...)             validate, apply, evaluate terminal, evaluate draws
4. append the move row         uq_move__ply refuses a duplicate ply
5. advance or complete         compare-and-set on ply_number
6. stage the outbox events     same transaction (AD-16)
7. commit  ──►  acknowledge
```

"Match advanced but move record missing" is impossible because 4 and 5 are one transaction.

**The acknowledgement is sent after the commit** (§7) and the commit is at the composition-root
boundary, not in the service — `open_session` rolls back on any path that does not reach it, so a
persistence failure propagates as an exception the gateway maps to a rejection rather than as an
accepted move.

**Concurrency**, three mechanisms, none process-local:

| Mechanism | Covers |
| --- | --- |
| The row lock (`FOR UPDATE`, no `SKIP LOCKED`) | Two players of one match. The second waits and then sees what the first wrote — a skipping lock would report "no such match" to somebody who has one |
| `uq_move__ply` | A duplicate ply, whatever took the lock. §2 forbids in-memory deduplication; this is the mechanism |
| `advance`'s compare-and-set | A match write whose ply moved, if a future path forgets the lock |

### 18.4 Terminal settlement

**Through `Match.play`, not around it.** The aggregate has done this since A64-014.7:

```
apply  ──►  TerminalStateEvaluator  ──►  DrawRuleSet
```

The evaluator is asked first and a win short-circuits, which is §5's "a decisive terminal result
must remain higher priority than a draw": a position can be both a third repetition and a win for
the side that just moved, and a game that was won is not a game that was drawn.

Reimplementing that sequence in the service would have been the duplication §5 forbids, and it
would have been a second copy of a priority rule that only shows its absence in a game somebody
loses. It also makes replay and live play the **same code path** — `ReplayEngine` calls
`match.play` too — so a replayed game reaches the result it reached when it was played by
construction rather than by agreement.

On settlement: `status = completed`, the result decomposed into three columns, `ended_at`, and
`game.match_completed` through the outbox. Further moves are refused with `match_not_active`,
checked **under the row lock**, so a move racing its own game's settlement is refused rather than
applied to a finished board.

### 18.5 Replay compatibility

`PersistedMatchReplay` builds `ReplayData` from the match row and the log. It is deliberately
tiny, because the log was designed to be replay-compatible rather than translated into
compatibility: `for_replay` already returns `MoveRecord`s, the variant fixes the opening position
deterministically, and §4's prohibition on persisting occurrence counts is what makes replay the
only way to get them — so replaying is not a fallback, it is the mechanism.

`expected_result` is filled for a completed match **so that a replay can fail**. A replay that
could not disagree with the record would prove nothing.

The live Redis position is now a **cache of a replay**. The log is the source; a cache miss costs
a rebuild rather than a game.

### 18.6 The remote transport bus

`GatewayBus` — `publish(BusMessage)` and `consume(node_id, limit)`. Framework-independent: no
FastAPI, no Redis, no socket.

`BusMessage` is primitive-only and JSON-round-trippable, which is what makes §9's "no direct
socket references in bus payloads" structural rather than remembered. **`request_id` and
`channel` travel inside the frame**, where the protocol already puts them — lifting them onto the
envelope would be a second copy of two fields that must not disagree.

| Property | Value |
| --- | --- |
| Delivery | At-least-once. The frame carries a ply and a client ignores a repeat |
| Ordering | None. The ply is the sequence, and the room projection refuses to move backwards |
| Failure | Returned, never raised — the move is committed before anything is delivered |
| Adapter today | `InProcessGatewayBus`. Complete against the contract, and does not cross a process boundary — correct for **single-node**, which remains the supported topology |
| Adapter next | `RedisStreamGatewayBus` on the `bus` role: a stream per node (`gwbus:v1:<node_id>`), `MAXLEN`-bounded, consumed by that node's own reader task |

### 18.7 What was still missing at A64-016.4, and where it went

| Gap | Closed by |
| --- | --- |
| No clocks | §19 (A64-016.5) |
| No multi-node bus adapter | §19.4 — the adapter in A64-016.5, the loop that drains it in A64-016.8 |
| No `UPDATE`/`DELETE` revocation on `game.move` | **Still open.** A privilege change, not a migration — `specs/live-game/audit.md` §7 |
| No draw thresholds | **Still open.** Three of four are undecided product rules — `specs/game-engine/audit.md` §8 |

---

## 19. Clocks — A64-016.5

AD-21: *"the clock is adjudicated by a worker against Redis"*, never by an in-process timer. A
timer lives on one node, and a node that is deployed takes every timer it held with it — those
matches then do not flag, they **hang**.

### 19.1 Where time is charged

`received_at`, captured at the frame boundary in `GatewayConnectionService._read_loop` and
nowhere later. MT-9 makes the gateway receive instant the temporal authority for the flag race,
and every step between that line and the clock — dispatch, the room read, the row lock, the
engine — is platform delay a player must not be charged for.

```
ClockState.charged(control, at)   charge the mover  ->  credit the increment
                                  ->  switch the side to move
```

Pure, in `game.domain.clock`, and therefore testable without a socket, a database or a sleep. A
flag is `at > deadline()` — **strictly** after, so a move arriving on the exact millisecond of
the deadline is in time.

### 19.2 The deadline keyspace

| | |
| --- | --- |
| **Key** | `clock:v1:deadlines` — one global sorted set, not one key per match |
| **Member** | `<match_id>\|<ply>\|<side>` |
| **Score** | The deadline, epoch milliseconds |
| **Instance** | `live` — a lost deadline is a match that never flags |
| **Owner** | `game` (`app/modules/game/infrastructure/clock_deadline_store.py`). Sole writer |

One set rather than one key per match because the question is *"which matches have flagged"*,
which a score range answers in one `ZRANGEBYSCORE`; per-match keys would need a scan.

**The ply is the version.** A move that beats the clock writes a deadline for the next ply and
supersedes the previous one in one Lua script, so there is no second sequence to keep in step —
the same number `uq_move__ply` already serialises on.

### 19.3 Adjudication

`ClockAdjudicationTask` on the **`realtime`** queue (AD-20), every
`GAME_CLOCK_INTERVAL_SECONDS`. One transaction **per deadline**, not per batch: two matches
flagging in the same pass are unrelated, and a batch transaction would let one match's failure
roll back another's settlement.

The race with a real move is closed by the row lock rather than by ordering:

```
claim expired deadlines   ->  SELECT ... FOR UPDATE the match
                          ->  is it still this ply, this side, still expired?
                          ->  yes: settle    no: superseded, drop it
```

A move that arrived first has already advanced the ply inside the same lock, so the adjudicator
finds a deadline that no longer describes the match and discards it. Neither path guesses.

`GAME_CLOCK_ENABLED=false` disables it, at `WARNING`, and the warning is load-bearing: with it
off moves still charge time and the log still records it, and **no match ever flags** — a game
nobody can lose on time, which is a silent product change rather than a degraded feature.

### 19.4 The cross-node bus, and the loop A64-016.8 added

`RedisStreamGatewayBus` on the **`bus`** role: one stream per node (`gwbus:v1:<node_id>`),
`MAXLEN ~`-bounded, `XREADGROUP` with one consumer group per node, `XACK` after decoding.

A64-016.5 shipped both halves and **no loop between them** — `consume` had no caller, so a frame
published for another node was written, reported delivered by `REMOTE_PUBLISHES`, trimmed, and
never seen. A64-016.8 added `GatewayForwarder` and `GatewayForwardingTask`
(`GATEWAY_FORWARDING_INTERVAL_SECONDS`, default 0.25s), and `FORWARDED_FRAMES` is the counter
that makes the round trip visible: `REMOTE_PUBLISHES` rising while it stays at zero is a fleet
whose nodes cannot talk to each other.

A scheduled pass rather than a blocking `XREADGROUP`, because a blocking read parks a Redis
connection for its whole duration and the failure when that connection drops is a node that
silently stops receiving — the exact defect the loop exists to fix. The cost is a bounded
worst-case latency of one interval.

---

## 20. State synchronisation and reconnection — A64-016.6

### 20.1 The snapshot

`game.public.MatchSnapshot` — one published read, so the gateway never assembles state from
`game` internals (R-7). It carries the position as a fingerprint **plus** a flat placement list,
because a client cannot render a board from a fingerprint and the gateway may not hold a
`Position`.

`sequence` **is the ply**. There is no second counter: it advances exactly when the position
changes, it is already under the match row's lock, and it is already what `uq_move__ply`
serialises on.

Deliberately absent: move history (the durable log answers that and is unbounded), and any
opponent profile — handles and ratings are `users`', composed by whoever renders them.

### 20.2 The replay buffer

| | |
| --- | --- |
| **Key** | `gwevent:v1:<match_id>` — a sorted set scored by the match sequence |
| **Member** | `<sequence>\|<frame>`, the frame being the exact bytes a live socket received |
| **Bounds** | `GATEWAY_EVENT_BUFFER_LENGTH` by rank, `GATEWAY_EVENT_BUFFER_TTL_SECONDS` on the key |
| **Instance** | `cache` — losing it costs a snapshot, not a game |

A sorted set rather than a stream, because the one operation this exists for is *"everything
after sequence N"*: a stream is keyed by its own entry ids, so answering that means keeping an
entry id per client — state per connection, in a store whose whole point is that connections are
disposable.

### 20.3 The recovery decision

```
last_known_sequence <= 0    ->  snapshot (the client is holding nothing)
client is current           ->  nothing
buffer proves continuity    ->  the missed frames, in order
buffer cannot prove it      ->  game.resync_required
```

**The rules are ordered, and the first two overlap.** A match nobody has moved in is at
sequence 0, and so is a client asking to start over, so `client is current` evaluated first
answers "you have missed nothing" to a client that has never held a position — the first resume
of every game. Zero means *holding nothing* regardless of the server's sequence; a client that
has genuinely seen a ply reports one or more and still takes the fast path.

**Continuity is proven by the oldest buffer entry, not by the count.** A buffer that has trimmed
past the client can still return frames, and returning them would leave that client missing plies
while believing it was current — the silent partial recovery A64-016.6 §6 forbids. `resync_required`
rather than an unrequested snapshot, so a client can count how often it falls behind.

### 20.4 Cross-node resume needs nothing special

Everything a resume touches is fleet-wide: the connection registry (`gwconn:v2:`), room membership
(`gwroom:v1:`), the buffer (Redis) and the snapshot (PostgreSQL). Nothing is process-local, so a
client reconnecting to a different node reads identical state. **No socket moves between nodes**,
and none needs to — the new node registers its own connection and joins the room by the ordinary
path.

---

## 21. Spectating — A64-016.7

Read-only, over the connection the client already holds, on AD-11's `game` channel. Not a second
socket, not a second broadcast pipeline and not a second snapshot format.

### 21.1 The keyspace

| | |
| --- | --- |
| **Key** | `gwspec:v1:<match_id>` — sorted set scored by the subscription's expiry |
| **Member** | `<player_id>\|<connection_id>` — a viewer with two tabs is two subscriptions |
| **Reverse index** | `gwspecconn:v1:<connection_id>` — a socket that dropped never says `spectator.leave` |
| **Expiry** | `GATEWAY_SPECTATOR_TTL_SECONDS`, by score, so a dead node's subscriptions drain with nothing sweeping |

### 21.2 Read-only is structural, not a check

A subscription lives in `gwspec:v1:` and **every** operation a spectator must not perform reads
`gwroom:v1:`. A spectator submitting a move is refused `not_in_room` by machinery that predates
spectating and knows nothing about it — stronger than a guard somebody has to remember to add to
the next handler.

### 21.3 Eligibility — an undocumented product decision, defaulted safely

Nothing on this platform specifies who may watch a game: no privacy flag, no tournament model, no
moderation surface. The shipped default, recorded in `app/gateway/spectators.py` and
[`specs/spectator.md`](../../specs/spectator.md):

| Rule | Why |
| --- | --- |
| The match is `active` or finished | A pairing still being accepted is not a game, and its existence is not public |
| Neither participant blocks them | BL-1 carries over unchanged: a blocker gains nothing if the blocked player can watch them play |
| Participants may not spectate | They are in the room already |

Everything else is permitted, which is the permissive direction and deliberate: a restrictive
default would ship the feature switched off with no way to switch it on without code.

**No delay is invented.** AD-10 permits a delayed spectator feed and names no interval; guessing
one would be inventing a competitive-integrity parameter. Frames are immediate, and the seam that
would hold one back is `RoomBroadcaster.deliver`.

### 21.4 One recipient set

Spectators are unioned into the existing fan-out, never served by a second one. Two things make
that safe:

- **`SPECTATOR_SAFE_EVENTS`**, checked inside `deliver` rather than by the caller. An allowlist,
  so the first participant-only frame anybody adds fails closed — the same mistake against a
  denylist would leak it.
- **The plan is restricted to the subscribed connection.** The router resolves every tab a player
  has open, which is right for a participant (they are in the room on all of them) and wrong for a
  viewer who pressed watch in one.

---

## 22. Participant commands — A64-020.5C-pre

Resigning and agreeing a draw. Four client frames, three server frames, one handler, one
application service, five columns.

### 22.1 Why the socket and not HTTP

AD-11 multiplexes one socket precisely so that cross-stream ordering is defined, and §13's
opening example is *"a resignation and a chat message sent in that order must arrive in that
order"*. A resignation on HTTP and a move on the socket would be two transports racing for the
same match, and the loser's meaning would depend on network timing.

So there is **no HTTP command endpoint**, and adding one later would reintroduce exactly the race
AD-11 exists to prevent.

### 22.2 The frames

| Direction | Frame | Payload | Notes |
| --- | --- | --- | --- |
| → | `game.resign` | `match_id` | |
| → | `game.draw.offer` | `match_id` | |
| → | `game.draw.accept` | `match_id` | |
| → | `game.draw.decline` | `match_id` | |
| ← | `game.draw.offered` | `match_id`, `offered_by`, `offered_at_ply`, `offered_at` | participants only |
| ← | `game.draw.declined` | `match_id`, `declined_by`, `ply` | participants only |
| ← | `game.completed` | `match_id`, `ply`, `result` | spectator-safe |
| ← | `game.command.rejected` | `code`, `reason` | correlated |

**`match_id` and nothing else.** No `player_id`, no `side`, no `winner`, no `outcome`, no
termination reason. The acting player is the socket's redeemed ticket resolved against the match's
seats by `MatchRecord.side_of`, so a frame naming a side is not something the protocol can
express — the same structural guarantee `game.move.submit` makes.

**The actor receives two frames**, as they do for a move: the authoritative event correlated to
their `request_id`, and the same event again as one of the room's recipients. That is what lets a
client advance its state from one code path whoever acted.

### 22.3 Why `game.completed` and not `game.resigned` / `game.draw.accepted`

A client's response to both is identical — render the result, stop accepting input — and
`termination_reason` already says which happened. Two frames would be two code paths that must not
diverge.

A move that ends a game does **not** send `game.completed`: the result rides on
`game.move.applied`, so the frame that shows the winning move is the frame that says it won.

### 22.4 Authorization

Three checks, cheapest first, and the order is the point:

```
rate limit        the shared move budget — a client cannot dodge it by
                  alternating moves with draw offers
room membership   one Redis read. This is what excludes a spectator: a
                  viewer is in the audience, never in the room
game.public       the database read and the row lock, reached only by a
                  participant who has joined
```

### 22.5 Durability and reconnect

A pending offer is **durable Match state**, on `game.match`:

| Column | Meaning |
| --- | --- |
| `draw_offer_by` | whose offer stands, or three nulls for none |
| `draw_offer_ply` | the ply it was made on |
| `draw_offer_created_at` | when |
| `light_draw_offer_from_ply` | the earliest ply LIGHT may open a **new** offer |
| `dark_draw_offer_from_ply` | the same for DARK |

Not Redis: the only role that could hold it is `cache`, which is configured to evict. Not the move
log: no move records an offer, so a `Match` replayed from the log would silently drop it.

On reconnect the offer arrives in `game.snapshot`, under a `draw` key carrying `offer`,
`may_offer`, `may_accept` and `may_decline` — resolved for the requesting viewer. A settled
resignation or agreed draw arrives as the snapshot's `result`, exactly as any other terminal state
does. **The client reconstructs nothing.**

### 22.6 Expiration

A pending offer is cleared when the recipient declines, the recipient accepts, the match
terminates for any reason, or **the recipient submits a move that is authoritatively applied**.

The last one happens inside the move's own transaction (`MatchRecord.after_move_by`, called from
`LiveMoveService._advance`), so the cleared offer and the ply that cleared it commit together. A
*rejected* move never reaches that point, so "a rejected move leaves the offer pending" is true by
construction rather than by a check.

An offer by the player who then moves is **not** cleared — playing on while the opponent thinks is
the ordinary way a draw is offered.

### 22.7 The re-offer rule

After a player's offer is resolved short of acceptance, they may not offer again until the
**opponent has completed one further move**. No wall clock, no Redis TTL, no in-process timer:
none of those survives a restart, and a player who reconnected would find their allowance
refreshed.

The mechanism is ply arithmetic. LIGHT moves on odd plies and DARK on even ones (`Match.play`
increments before appending, and LIGHT moves first), so at resolution time the threshold is *the
earliest ply the recipient could next move on*:

```
threshold = P + 1  if (P+1) has the recipient's parity, else P + 2
eligible  ⟺  ply_number >= threshold
```

Both cases fall out of the one formula:

| Resolution | At ply | Threshold | Effect |
| --- | --- | --- | --- |
| Declined | 3 | 4 | the opponent's next move |
| Cleared by the opponent's move | 4 | 6 | one move **beyond** the one that resolved it |

A player's own move never advances their eligibility, which is the spam this prevents.

### 22.8 Clocks

A resignation or an accepted draw cancels the match's deadline. That is an optimisation, not the
correctness mechanism: `ClockAdjudicationService._is_current` checks `status is ACTIVE` against
the authoritative row under its lock, so a settled match makes every outstanding deadline token
non-current regardless. A stale flag worker loses.

**An offer and a decline do not touch the clock.** A player who offers a draw is still on it — the
alternative is a free pause available on demand.

### 22.9 Spectator visibility

`SPECTATOR_SAFE_EVENTS` gains `game.completed` and deliberately **not** the two draw frames. A
negotiation two players are holding is theirs until it produces a result; a finished game is
public.

The allowlist alone would not be enough, because a viewer could read the same offer from a
snapshot. So the projection is split in two: `spectator_snapshot_payload` is physically incapable
of carrying draw state, and `participant_snapshot_payload` is the only one that does. Two
functions rather than a flag, because a flag defaults to something and the default that leaks is
the one that ships.

### 22.10 Exactly one terminal result

Guaranteed by the row lock plus the status check, not by a distributed lock:

- `matches.lock` takes `FOR UPDATE` without `SKIP LOCKED`, so two players acting at once queue and
  the second sees what the first wrote.
- Every transition refuses a match that is not `ACTIVE`, and a completed match is not active.
- `advance`'s compare-and-set on `(ply_number, status = active)` holds if a future path reads
  without locking.

So no match can end both by resignation and by agreed draw, and exactly one `MatchCompleted`
reaches `rating`, `statistics` and `tournaments` — the same event a move-terminated game
publishes, with a different `termination_reason`. There is no new rating path.

### 22.11 Error codes

`draw_offer_already_pending`, `draw_offer_not_pending`, `draw_offer_not_recipient`,
`draw_offer_not_allowed_yet`, plus the existing `match_not_active`, `not_in_room`,
`not_a_participant`, `stale_state`, `rate_limited` and `internal_error`.

`draw_offer_not_allowed_yet` is distinct from `rate_limited` because it is not about how fast the
client is asking: the answer changes when the opponent moves, not when a window elapses.

`MatchNotFound` and `NotAMatchParticipant` share `not_a_participant`, so live match identifiers
cannot be enumerated by sending resignations at them.

### 22.12 Not in this phase

| Deferred | Why |
| --- | --- |
| Withdrawing an offer | §1 excludes it from v0.x. The recipient's move already resolves one, so the offerer's escape hatch exists |
| Takebacks | No frames, no domain concept, no product decision |
| Adjourn / abort by agreement | `TerminationReason.ABORT` exists but nothing specifies who may ask |

---

## 23. Matchmaking push and participant draw state — A64-020.5D

### 23.1 `matchmaking.match.offered`

A pairing, addressed to one participant, on the `matchmaking` channel.

**An optimisation, never the source of truth.** The durable answer is
`GET /matchmaking/matches/pending`, and every failure mode is tolerable by
construction:

| Failure | Recovered by |
| --- | --- |
| Nobody connected | The read, when they come back |
| Duplicated | Client dedupes by `match_id`; a duplicate costs nothing |
| Arrives late | The read has already told them, or is about to |
| Missed while disconnected | The read on reconnect |
| Publish raised | Counted; the relay tick is not failed |

So `GatewayPendingMatchSink.deliver` **never raises**, which is a deliberate
departure from `PendingMatchSink`'s "a sink may raise". That contract exists
so a real transport failure is retried; here a retry would re-deliver an
offer the client can already read, and failing the tick would hold up every
other offer in the batch. A match pairing must never be undone because a
socket was busy.

**Addressed, not broadcast.** Each participant's offer names *their* side and
*their* view of the opponent, so `recipients=[offer.recipient_id]` — a
fan-out to both would hand each player the other's card.

The payload carries exactly the fields `PendingMatchResponse` does, so a
client parses one shape whether it was pushed or polled. No ticket, no
pairing id, no Redis key, no ORM row, no rating, no email.

### 23.2 Cross-node delivery

Through the existing `RoomBroadcaster` → `FleetConnectionRouter` →
`gwbus:v1:<node>` → `GatewayForwarder` path, unchanged. An offer created on
node A for a player connected to node B is written to B's `MAXLEN`-capped
stream and drained by B's forwarder.

`build_broadcaster_for` assembles that graph without a request, because the
relay consumer has none. It uses the **process-wide** `get_local_sockets()`,
the same map the connection lifecycle writes into — a second instance would
be a fan-out that silently reaches nobody on this node.

### 23.3 Metrics

`gateway.match_offer_pushes_total`, labelled `outcome` ∈ {`local`, `remote`,
`no_connection`, `failed`}. Bounded by construction: no player, match,
connection or pool label.

`no_connection` is **not** a failure — it is the ordinary state of a player
who queued and closed the tab. A rising share of it is the number that says
whether push is worth having, and it is meaningless without the total.

### 23.4 `game.draw.state`

One participant's draw agreement: whether an offer stands, whose it is, and
which of the three actions they may take.

**Participant-targeted, and that is the whole reason it exists.**
Permissions are per-seat, so they cannot ride on `game.move.applied`, which
fans out to both players *and* the audience. A64-020.5C worked around the
gap by re-reading a whole snapshot once per ply for a restricted player;
this replaces that.

Published after a draw command and after an applied move — the two things
that can change an agreement. Skipped entirely while `is_untouched`, so a
game nobody negotiates in costs zero extra frames.

> `is_untouched` rather than "the agreement is currently quiet". The earlier
> predicate was wrong in the one case that mattered: after a decline, the
> opponent's move restores the offerer's eligibility, and at that moment the
> agreement *is* quiet — so a skip-when-quiet rule suppressed exactly the
> frame carrying the good news. Found by the two-browser flow.

Carries **no ply and no sequence**. It is not a state change of the game, so
it is never buffered into the ply-sequenced replay buffer — that would break
the contiguity check a resume depends on.

### 23.5 Ordering

A move that clears an offer produces two frames, in this order:

```
game.move.applied     the board, to participants and spectators
game.draw.state       the permissions, to each participant separately
```

The board first, so a client never briefly shows an offer cleared by a move
it has not yet seen. **The client must still tolerate either order**, and
does: `game.draw.state` replaces the agreement and touches neither board,
clock, turn nor sequence, so applying it early is harmless and the next
snapshot reconciles regardless.

### 23.6 Spectator exclusion

Two independent mechanisms, and both are needed:

- `game.draw.state` is sent with `recipients=[one participant]` and **no**
  `spectators` argument at all, so the allowlist never has to withhold it.
- `spectator_snapshot_payload` is physically incapable of carrying a `draw`
  block, so a viewer cannot read the same state by joining and taking a
  snapshot.

### 23.7 Known limitation: activation is not pushed

`matchmaking.match.offered` fires on `match_created`. **Nothing pushes
activation** — when the opponent accepts and the match becomes `active`, the
first acceptor learns by reading.

So an open offer keeps the two-second interval whatever the socket is doing,
which is `websocket.md`-visible because it is a protocol gap rather than a
client choice. It is narrow: it applies only while an offer is open, which
lasts at most the acceptance window. Closing it means a
`matchmaking.match.activated` frame, and it is the obvious next increment.

---

## Related Documents

| Document | Relationship |
| --- | --- |
| [`architecture.md`](./architecture.md) | AD-09 through AD-12, R-7 — the decisions this realises |
| [`caching.md`](./caching.md) | The `wsticket:v1:` and `gwconn:v1:` keyspaces in the namespace registry |
| [`security.md`](./security.md) | Credential handling; the ticket is a DB-24 opaque value |
| [`specs/matchmaking.md`](../../specs/matchmaking.md) | §11.4's pending-match delivery, whose sink this tier eventually becomes |
