# Realtime / WebSocket Architecture

> **Status:** Approved for what exists — the authenticated connection foundation (A64-016.1)
> and game room sessions (A64-016.2). Move routing, clocks, spectators, reconnection replay
> and the cross-node transport are specified in outline only and are marked as such.
> **Owner:** _Unassigned_
> **Last reviewed:** 2026-08-02 (A64-016.2)
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
| Move routing, clocks, spectators, chat | **Deferred** — §9 |
| Cross-node message *transport* | **Deferred** — §9, §14.3 |
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
| `error` | the failing frame's | server → client | A refusal, carrying a code |

Eight, and every one of them is implemented. Adding a type without the handler behind it is the
speculative generality CLAUDE.md §1.7 forbids, and the platform has declined it three times now
— `TokenType.ACCESS` alone, `PasswordHasher.hash` alone, and A64-016.1's four-member
`MessageType` — on the grounds that an unused member on a protocol surface reads as "this is
wired up" to whoever adds the next task.

**Still not a dispatch table.** The handler is four branches reading top to bottom in the order a
connection meets them. A table would add a registration step and an indirection to save nothing;
it earns its place when a handler needs its own collaborators, which is A64-016.3's move
submission.

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

**No player ids, no ticket ids, no connection ids, no match ids and no node ids in any label.** Beyond A64-015.5 §9's
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

There is **no kill switch**. Presence and the friends cache have one because they degrade to a
working platform with a feature missing; a gateway with no registry cannot answer "does this
player have another connection", so presence would flap on every closed tab. A switch whose off
position is "broken" is not a switch — turning the gateway off is a deploy decision.

---

## 12. Known gaps

| Gap | Consequence today | What closes it |
| --- | --- | --- |
| ~~No contract suite over `wsticket:v1:` or `gwconn:v1:`~~ | **Closed by A64-016.2.** `tests/contract/test_gateway_redis.py` proves `GETDEL` single-use, concurrent redemption with one winner, atomic counting across register/unregister, and node resolution — against real Redis | — |
| No contract suite over `gwroom:v1:` | Room membership and the reverse index are asserted against a fake that models them. The atomicity of "add then read the members" is Redis's | Extend `test_gateway_redis.py`. Cheaper than the connection suite was, because the fixture now exists |
| `ConnectionRouter.plan_for` has no caller | The seam is built and tested and nothing routes through it. Deliberate (§14.3), and it is dead code until A64-016.3 | The move fan-out |
| The `/ws` route itself is not driven by a test | Three statements — accept, wrap, delegate — and a mis-wiring fails at application build. Verified by hand during A64-016.1 | One `TestClient.websocket_connect` case, once the suite has a websocket fixture |
| No per-connection rate limit | One authenticated client can send frames as fast as it can write them. Bounded per frame, not per second | A limiter on the read loop, once there is a message worth spending |
| No node identity in the registry | Cross-node message routing is impossible | `gwconn:v2:` — see §9 |

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

## Related Documents

| Document | Relationship |
| --- | --- |
| [`architecture.md`](./architecture.md) | AD-09 through AD-12, R-7 — the decisions this realises |
| [`caching.md`](./caching.md) | The `wsticket:v1:` and `gwconn:v1:` keyspaces in the namespace registry |
| [`security.md`](./security.md) | Credential handling; the ticket is a DB-24 opaque value |
| [`specs/matchmaking.md`](../../specs/matchmaking.md) | §11.4's pending-match delivery, whose sink this tier eventually becomes |
