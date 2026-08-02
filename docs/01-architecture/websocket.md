# Realtime / WebSocket Architecture

> **Status:** Approved for what exists — the authenticated connection foundation (A64-016.1).
> Rooms, move routing, clocks, spectators and reconnection replay are specified in outline
> only and are marked as such.
> **Owner:** _Unassigned_
> **Last reviewed:** 2026-08-02
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
| Message envelope and the four message types | **Built** |
| Rooms, move routing, clocks, spectators, chat | **Deferred** — §9 |
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

## 5. Connection registry — §3 of the task

### 5.1 The keyspace

```
gwconn:v1:<player_id>  ->  sorted set: member = connection_id, score = expiry epoch
```

Owned by the gateway, written only by the gateway (caching.md C-8), on the **`cache`** Redis
role. Every operation reaps expired members first, so the count is of what is genuinely live.

### 5.2 Why a sorted set and not a counter

`INCR` on connect and `DECR` on disconnect is smaller, faster, and wrong in the one way that
matters: **it cannot be repaired.** A gateway node killed mid-deploy never runs its decrements,
so the counter is permanently too high and its players are online forever — and nothing can tell
a leaked increment from a real connection.

Scoring each connection by its expiry makes the structure self-healing: a dead node's entries
fall out of every count on the next operation by *any* node, without coordination and without a
sweeper. Same argument `presence:v1:` makes for a TTL over a swept row, applied to a value that
holds more than one thing.

### 5.3 Why the counts come back from the writes

`register` returns the live count including the new connection; `unregister` returns what
remains. Both from a single `MULTI`/`EXEC`.

That is what makes multi-tab presence correct across a fleet. The alternative — write, then read
the count — has a window that another node's connect or disconnect lands in, so two closing
sockets can both read zero and take a connected player offline. Reading the count *from* the
write removes the window: exactly one caller ever sees `1` on the way up and exactly one ever
sees `0` on the way down.

---

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
{ "v": 1, "type": "pong", "payload": {}, "request_id": "beat-17" }
```

| Field | Required | Notes |
| --- | --- | --- |
| `v` | yes | `PROTOCOL_VERSION`. A frame with any other value is refused — AD-11 multiplexes one socket, so a client has to be able to say what it speaks before the server sends something it cannot parse |
| `type` | yes | A member of the closed set in §8.2. An open string would be a router dispatching on whatever a client sends |
| `payload` | no | An object. Defaults to `{}` |
| `request_id` | no | Echoed back on the response. A non-string or one over 64 characters is **dropped rather than rejected** — the field is a courtesy, and echoing an unbounded client-supplied value is an amplification primitive |

`request_id` is carried and echoed **now**, before anything needs it, because the round trip that
first needs it (A64-016.2's move submission, and AD-23's optimistic board matching a confirmation
to the move it confirms) would otherwise have to change the envelope for every existing type at
once.

### 8.2 The four types

| Type | Direction | Meaning |
| --- | --- | --- |
| `connection.ready` | server → client | Sent once, immediately after redemption. The signal that the socket is **authenticated**, not merely open — without it a client cannot distinguish "connected and trusted" from "connected and about to be closed" |
| `ping` | client → server | Heartbeat |
| `pong` | server → client | Heartbeat answer, correlated |
| `error` | server → client | A refusal, carrying a code |

Deliberately four. Adding a live-game type without the handler behind it is the speculative
generality CLAUDE.md §1.7 forbids, and the platform has declined it twice before —
`TokenType.ACCESS` alone and `PasswordHasher.hash` alone, both on the grounds that an unused
member on a security or protocol surface reads as "this is wired up" to whoever adds the next
task.

### 8.3 Errors carry a code, never prose

| Code | Meaning |
| --- | --- |
| `invalid_ticket` | Nothing redeemable was presented. The connection closes immediately after |
| `malformed_message` | Not JSON, not an object, too large, unknown type, or the wrong protocol version. **The connection stays open** |
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
| Game rooms and channel multiplexing | A64-016.2 | AD-11 — **one** socket per client, multiplexed by channel. Not a second connection |
| Move submission and acknowledgement | A64-016.2 | R-7 — the gateway routes; `game` decides legality. The envelope's `request_id` is already the correlation token |
| Player/spectator channel split | Later | AD-10 — separate channels with independent policy, so a spectator feed can be delayed without touching player latency |
| Clocks | Later | AD-21 — adjudicated by a worker against Redis, never by an in-process timer on one gateway node |
| Reconnection replay | Later | AD-12 — per-match sequence numbers; the client sends its last-seen value and the gateway replays the gap from a bounded stream |
| Cross-node fan-out | A64-016.2 | The registry holds `player_id → connection_id`; routing to the *node* holding a socket needs the node identity beside it, which is a `gwconn:v2:` shape rather than a wider value |
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

**No player ids, no ticket ids, no connection ids in any label.** Beyond A64-015.5 §9's
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

There is **no kill switch**. Presence and the friends cache have one because they degrade to a
working platform with a feature missing; a gateway with no registry cannot answer "does this
player have another connection", so presence would flap on every closed tab. A switch whose off
position is "broken" is not a switch — turning the gateway off is a deploy decision.

---

## 12. Known gaps

| Gap | Consequence today | What closes it |
| --- | --- | --- |
| No contract suite over `wsticket:v1:` or `gwconn:v1:` | `GETDEL`'s single-use guarantee and the registry's atomic count are asserted against fakes that model them. A model that agrees with itself proves nothing about Redis | A contract suite with two real connections, on the pattern of `tests/contract/test_outbox_repository.py`. **The first thing A64-016.2 should add** |
| The `/ws` route itself is not driven by a test | Three statements — accept, wrap, delegate — and a mis-wiring fails at application build. Verified by hand during A64-016.1 | One `TestClient.websocket_connect` case, once the suite has a websocket fixture |
| No per-connection rate limit | One authenticated client can send frames as fast as it can write them. Bounded per frame, not per second | A limiter on the read loop, once there is a message worth spending |
| No node identity in the registry | Cross-node message routing is impossible | `gwconn:v2:` — see §9 |

---

## Related Documents

| Document | Relationship |
| --- | --- |
| [`architecture.md`](./architecture.md) | AD-09 through AD-12, R-7 — the decisions this realises |
| [`caching.md`](./caching.md) | The `wsticket:v1:` and `gwconn:v1:` keyspaces in the namespace registry |
| [`security.md`](./security.md) | Credential handling; the ticket is a DB-24 opaque value |
| [`specs/matchmaking.md`](../../specs/matchmaking.md) | §11.4's pending-match delivery, whose sink this tier eventually becomes |
