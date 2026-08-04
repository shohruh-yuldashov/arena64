# Spectator

> **Status:** Implemented — A64-016.7. The eligibility policy is a **defaulted product
> decision**, not a specified one; see §3.
> **Owner:** _Unassigned_
> **Related:** [`docs/01-architecture/websocket.md`](../docs/01-architecture/websocket.md) §21,
> [`specs/live-game/audit.md`](./live-game/audit.md) §4, architecture.md AD-10, AD-11
> **Code:** `apps/api/app/gateway/spectators.py`, `spectator_handler.py`, `spectator_store.py`

## 1. Description

Watching a live or finished match, read-only, over the WebSocket connection the client already
holds. A spectator receives the position when they join and every subsequent move as it is
played.

## 2. Protocol

| Message | Direction | Payload | Notes |
| --- | --- | --- | --- |
| `spectator.join` | Client → server | `match_id` | Channel `game`. **No player field** — the viewer is the socket's redeemed ticket |
| `spectator.joined` | Server → client | The snapshot, plus `audience` | The same projection a reconnecting participant receives |
| `spectator.leave` | Client → server | `match_id` | Idempotent |
| `spectator.left` | Server → client | `match_id` | Sent whether or not the connection was watching |
| `game.move.applied` | Server → client | The move | The only event on the allowlist today |

| Refusal | Code | Covers |
| --- | --- | --- |
| Unknown match, or one not being played | `not_spectatable` | **Both**, so live match identifiers stay unenumerable |
| Blocked, or is a participant | `spectating_forbidden` | Both, so a client cannot probe the block graph |

## 3. Eligibility — the open product decision

**Nothing on this platform specifies who may watch a game.** There is no `show_spectators`
privacy flag, no tournament model and no moderation surface. A64-016.7 §1 requires the safest
minimal default to ship and be reported rather than a policy to be guessed, and this is that
default:

| Rule | Rationale |
| --- | --- |
| The match is `active` or finished | A pairing still being accepted is not a game, so there is nothing to watch and its existence is not public |
| Neither participant blocks the viewer | BL-1's reasoning carries over: a blocker gains nothing if the person they blocked can watch them play. Symmetric, because `PairingExclusions.blocked_pairs_among` is |
| Participants may not spectate | They are in the room already, and a participant on the spectator channel would receive every event twice |

Everything else is permitted. That is the **permissive** direction and it is deliberate: a public
game platform's default is that games are watchable, and the restrictive default would ship the
feature switched off with no way for a product decision to switch it on without a code change.

**A block that cannot be checked is treated as a block.** A read failure refuses, because
admitting on error would make a database blip a privacy bypass.

### 3.1 What is deliberately not invented

**A delay.** AD-10 says a spectator feed *can* be delayed and gives the reason — engine assistance
relayed to a player in real time — but names no interval. Guessing one would be inventing a
competitive-integrity parameter, so frames are immediate today and the seam that would hold one
back is `RoomBroadcaster.deliver`.

## 4. What a spectator cannot do

Enforced **structurally** rather than by guards. A subscription lives in `gwspec:v1:` and every
one of these reads `gwroom:v1:`:

| Action | Refused by |
| --- | --- |
| Submit a move | `MoveSubmissionHandler` → `not_in_room` |
| Accept or decline a match | HTTP, behind `CurrentUser` |
| Alter a clock | Written only by the move transaction and the adjudication worker |
| Join as a participant | `GameRoomService` → `not_a_participant` |

## 5. Storage

Ephemeral by construction — no PostgreSQL entity, and no durable `SpectatorSession`. A
subscription is a claim about a socket that exists right now, derived from nothing durable and
reconstructible by the viewer pressing watch again. Keyspaces are in
[`caching.md`](../docs/01-architecture/caching.md) §8.1.

## 6. Open decisions

| Decision | Owner | Consequence of leaving it |
| --- | --- | --- |
| Whether a delay applies, and how long | Product | AD-10's engine-assistance concern is unmitigated for spectated games |
| Whether players may opt out of being watched | Product | A player who does not want an audience has no way to say so |
| Whether a finished match stays watchable forever | Product | Today it does, bounded only by the match row's own retention |
| Audience size limits | Product | A fan-out is one write per viewer per move, unbounded above |
