# Feature Specification — Quick Messages

| Field | Value |
| --- | --- |
| **Spec ID** | `SPEC-023` |
| **Status** | Implemented — A64-023.1 (domain, contracts, architecture) and A64-023.2 (picker, incoming presentation, match-scoped mute). |
| **Owner** | _Unassigned_ |
| **Created** | 2026-08-09 |
| **Last updated** | 2026-08-09 |
| **Related ADRs** | [`ADR-004`](../docs/07-decisions/ADR-004-quick-messages-not-free-text-chat.md) |
| **Related specs** | [`chat.md`](./chat.md) (deferred), [`spectator.md`](./spectator.md), [`frontend.md`](./frontend.md) |
| **Related docs** | [`websocket.md`](../docs/01-architecture/websocket.md) §20 |
| **Code** | `apps/api/app/gateway/quick_messages.py`, `quick_message_handler.py`, `quick_message_limits.py`; `apps/web/src/features/game/model/quick-messages.ts`, `use-quick-messages.ts`, `ui/quick-message-picker.tsx`, `ui/quick-message-bubble.tsx` |

---

## 1. Summary

During a live match, either player may send the other a **quick message** — one of a small,
fixed set of courtesies chosen by Arena64, such as "good luck" or "good game". The player
picks from a list; they never type. The message appears on the opponent's screen in the
opponent's own language.

There is no free-text chat on Arena64, and this feature is deliberately not a step toward
one. See [`ADR-004`](../docs/07-decisions/ADR-004-quick-messages-not-free-text-chat.md) for
the decision and the conditions under which it would be reopened.

## 2. Motivation

Players on every board game platform say "gg" at the end of a game; a product that provides
no way to do it is read as unfriendly, and players route around it — putting messages in
usernames, or moving contact off-platform where nothing can be enforced.

The obvious answer, chat, cannot ship: it needs reactive moderation, and Arena64 has no
moderation capability. A fixed catalogue gives players the thing they actually want while
making abuse **impossible by construction** rather than punishable after the fact.

## 3. Goals

- G-1 — Either participant of a live match can send any catalogue entry to the other.
- G-2 — The receiving player reads it in their own locale, in all three supported languages.
- G-3 — No user-authored text is transportable, at any point, in any field.
- G-4 — Correct game state never depends on a quick message being sent, delivered or lost.
- G-5 — Catalogue entries can be added or removed without a protocol change.
- G-6 — No storage grows in proportion to quick-message traffic.

## 4. Non-Goals

| Not built | Where it belongs |
| --- | --- |
| Free-text chat, in any scope | Nowhere — ADR-004 |
| Direct messages between friends | A future spec, and not this frame |
| Message history, inbox, or transcripts | Nothing persists; there is no history to show |
| Spectator communication | ADR-004; spectators are an audience |
| Tournament, guild or team communication | Out of scope |
| Moderation queue, reports, sanctions | `admin`, unbuilt |
| Translation of user text | There is no user text |
| Read receipts, typing indicators, delivery acknowledgement | §6 — delivery is best effort |

## 5. The catalogue

Six entries. The **member name is the identity**, the value is the wire form, and the
display text belongs to the client.

| Identifier | Wire value | When it is for |
| --- | --- | --- |
| `GOOD_LUCK` | `good_luck` | Before the first move |
| `NICE_MOVE` | `nice_move` | During play; the only entry about the board |
| `WELL_PLAYED` | `well_played` | At the end, about how the opponent played |
| `GOOD_GAME` | `good_game` | At the end, about the game |
| `THANKS` | `thanks` | The reply that lets an exchange terminate |
| `OOPS` | `oops` | The sender's own blunder or misclick |

**Why six, and why these.** Each covers a moment a draughts game actually has, and the set
is closed under one property that is the whole abuse model: **every entry is positive or
neutral by construction.** A taunt cannot be sent because no taunt exists to send.

A gap in the catalogue is not neutral — a player who cannot say the ordinary thing looks
for somewhere else to say it, and there must be no somewhere else. That is the argument
against trimming below six. The argument against sixty is that every entry is three
translations and one more thing that can be sent at the wrong moment.

**`WELL_PLAYED` and `GOOD_GAME` are kept apart deliberately.** They overlap in English and
not in usage: one is about the opponent, one is about the game, and folding them would make
the courtesy at the end of a loss read as praise the loser may not mean.

**Emoji are not a second concept.** A reaction and a quick message are identical on the
wire — a closed identifier, from a participant, about one live match, rendered by the
receiver — so two concepts would be two handlers, two rate limits and two authorization
paths that must not diverge. A client is free to render any entry with a glyph;
presentation is the client's. An entry that is *only* a glyph is a future catalogue member,
not a future protocol.

**No pure-emoji reactions in the initial catalogue**, and that is a safety decision rather
than an oversight: a laughing or shrugging glyph after an opponent's blunder is a taunt with
deniability, and this platform has nothing to adjudicate one.

## 6. Semantics

| # | Question | Answer | Why |
| --- | --- | --- | --- |
| S-1 | One protocol concept or two? | **One** | Reactions and messages are the same thing on the wire — §5 |
| S-2 | One catalogue or two? | **One** | Same |
| S-3 | Persisted? | **No** | Nothing reads it. Persisting creates a retention and erasure obligation with no consumer, and storage proportional to spam |
| S-4 | In match history or replay? | **No** | Replay is derived from the move log and is engine-versioned. A quick message is not a ply |
| S-5 | Replayed on reconnect? | **No** | The replay buffer is keyed by match sequence; a non-ply entry breaks the contiguity check a resume proves its gap with |
| S-6 | Recoverable if sent just before a disconnect? | **No** | Ephemeral. Losing one is acceptable; guaranteeing it is not worth the architecture |
| S-7 | After a terminal state? | **Refused** — `match_not_active` | Post-game abuse is the largest source of reports on competitive platforms. The rule `domain-model.md` CT-1 stated for match chat, kept for what replaced it |
| S-8 | Before the first move? | **Permitted** | A match is `active` from acceptance, before ply 1 — which is when `good_luck` means something |
| S-9 | While the opponent is disconnected? | **Permitted, and lost** | Arena64 has no paused match state; a match stays `active`. The frame reaches whatever sockets exist, which may be none |
| S-10 | Can a player message themselves? | **Structurally impossible** | The frame has no recipient field. Recipients are the roster's two seats, derived server-side |

**Ordering.** A quick message is stamped with the gateway's receive instant — the same
authority a move is stamped with — so a message and the move beside it cannot disagree about
which happened first. Both players see the same instant; a client-supplied timestamp is
ignored.

## 7. Authorization

Checked in this order, cheapest first. Every step is a refusal a client can branch on.

| # | Check | Refusal | Notes |
| --- | --- | --- | --- |
| A-1 | The socket is authenticated | connection refused at handshake | AD-09's ticket. The sender is **never** read from the payload |
| A-2 | The connection's rate budget | `rate_limited` | §8. First, so it protects everything after it |
| A-3 | The frame names a match and a catalogue entry | `unknown_quick_message` | In-memory; no store is touched for a frame that names nothing valid |
| A-4 | **This connection** is in the match's room | `not_in_room` | One Redis read. This is what excludes spectators — a viewer is in the audience, never in the room |
| A-5 | The sender is on the roster | `not_a_participant` | Authoritative. One code for "no such match" and "not yours", so match identifiers are not enumerable by response |
| A-6 | The match is `active` | `match_not_active` | S-7. **Room membership is not sufficient**: a room outlives the match that made it |

**A-6 is the check most likely to be dropped by a future refactor and the one that matters
most.** `ROOMABLE_STATES` is evaluated when a connection *joins*; membership then persists
until the connection leaves or the TTL lapses. A handler that stopped at A-4 would carry
conversation into a finished game, silently.

**Recipients are derived, never supplied.** They are the roster's two seats. Cross-match
delivery is impossible because the only match reachable is the one whose room the sender was
already proven to be in, and there is no field on the frame that could name anyone else.

**Spectators receive nothing.** Two independent mechanisms, either of which alone would
suffice: `game.quick_message.received` is absent from `SPECTATOR_SAFE_EVENTS`, and the
handler passes no audience to the fan-out at all.

## 8. Rate limits and the abuse boundary

Per **connection** (`RateLimitScope.CONNECTION`), not per player: a player with two tabs is
two clients, and a shared bucket would let one tab's misbehaviour throttle the other.

| Rule | Default | Setting |
| --- | --- | --- |
| Burst | 3 per 10 seconds | `GATEWAY_QUICK_MESSAGE_BURST_LIMIT`, `…_BURST_WINDOW_SECONDS` |
| Sustained | 6 per 60 seconds | `GATEWAY_QUICK_MESSAGE_RATE_LIMIT`, `…_RATE_LIMIT_WINDOW_SECONDS` |
| Kill switch | on | `GATEWAY_QUICK_MESSAGE_RATE_LIMIT_ENABLED` |

Both are spent in **one atomic acquisition**, so the burst bucket is never charged for a
send the sustained rule then refuses.

Neither rule alone is sufficient. Burst alone permits a message every three seconds forever
— somebody typing at their opponent for an hour. Sustained alone permits six in one second
and then silence, which is the flood a recipient actually experiences.

**Its own budget, not the move handler's.** Sharing would let a player who spams messages
consume the allowance their moves need, so the punishment for being annoying would be losing
on time. A social channel must never starve the gameplay one.

**Deferred to A64-023.3:** duplicate suppression — the same identifier repeated. At six a
minute the repetition it would prevent is already bounded to something a recipient can
ignore, and the enforcement point is a per-connection last-sent key in Redis, never a
database write.

## 9. Mute — as built in A64-023.2

A player may silence their **opponent's** quick messages for the current match.

| Property | Behaviour | Why |
| --- | --- | --- |
| Scope | This match, this tab, this session | Nothing is persisted and no preference row exists. A mute is a reaction to one opponent in one game, not a standing setting |
| What it affects | The **presentation** of the opponent's messages, and nothing else | Moves, clocks, resign, draw offers, the result and the socket are untouched — §17 |
| Own messages | Still shown, through the server echo | A player muting an opponent has not asked to stop seeing what they themselves sent |
| On enabling | The opponent's visible bubble is cleared immediately | Otherwise the mute appears not to have worked until the next message |
| On disabling | Nothing replays | Suppressed messages are dropped on arrival, never queued. Mute applies **prospectively** |
| Control | A toggle beside the picker, carrying `aria-pressed` | The state, not only the label, is available to assistive technology |

**Deliberately client-side.** The suppression changes nothing the server must
guarantee, needs no round trip, and a server-side per-recipient filter would put a
preference read on every fan-out. Nothing about this choice is load-bearing: the
server-side seam below remains where it was.

### 9.1 Blocks — the seam, still unimplemented

`ADR-004` and §7 of the original design split the two: an ordinary **mute** is the
client's, a **BL-1 block** must be the server's, because BL-1 requires the sender not
be told.

**Server-side block suppression is not built.** `QuickMessageHandler` holds
`MatchRosterReader`, `GameRoomService`, `RoomBroadcaster`, a limiter and metrics — and
no `friends.public.PairingExclusions`. The seam is exactly one function,
`QuickMessageHandler._recipients_of`, which is the only place a recipient list exists.

It is left unbuilt deliberately rather than by oversight:

- **BL-2 already prevents the case.** Blocked pairs are excluded from pairing, so the
  only way two live opponents can have a block between them is one placed *mid-match*.
- **It would put a social-graph read on the hot path** of every quick message, for that
  case alone.
- Building it means wiring `friends` into a handler that currently reaches exactly one
  module, which is a decision worth making on purpose rather than as a side effect of a
  UI phase.

Recorded as OQ-3.

## 10. Localization

| Owns | Party |
| --- | --- |
| The catalogue of identifiers | Server |
| Which identifier was sent | Server |
| The display text for an identifier | **Receiving client** |
| Which language to render in | **Receiving client** |

The server never sends prose. `apps/web/src/shared/i18n/locales/{uz,ru,en}.json` hold the
labels under `game.quickMessages`, and the receiving client renders the incoming identifier
through its own `useTranslation`. Two players in one match may therefore be reading the same
frame in two languages, which is the point — Arena64's primary audience reads Uzbek.

A fourth locale is three JSON edits and no server change.

**The one thing that could silently break** is a catalogue entry added without labels: the
client's `lookup` returns the key when it does not resolve, so the frame would render as the
literal string `game.quickMessages.sorry`. `tests/contract/test_quick_message_localisation.py`
asserts the direction the risk runs — every catalogue member has a non-empty label in every
supported locale.

Refusal *reasons* are server-authored English, as they already are for moves and commands.
Clients branch on `code`; the sentence is a fallback. The **message** is never prose.

## 10a. The client experience — A64-023.2

### Picker

A compact **non-modal menu** beside the game controls: a trigger, the six catalogue
entries, and nothing else. There is no text input, textarea or editable element
anywhere in it, which is what makes free text unsendable from the UI as well as from
the protocol.

Hand-written rather than taken from a library: this repository has no popover or menu
primitive, and the only alternative was a new dependency. A *dialog* was available and
deliberately not used — `ResignDialog` is modal, which is right for a destructive
confirmation and wrong for saying "nice move" while a clock runs.

Each entry carries a restrained glyph which is **presentation only**: the protocol
value is the identifier, the glyph is `aria-hidden`, and no entry is capable of a
taunt. Adding a glyph never adds a catalogue entry.

### Incoming presentation

A small bubble rendered **inside the panel, adjacent to the seat that sent it** — the
opponent's above their clock, the viewer's own below theirs. Not an overlay and not a
global toast: the panel already orders the two seats, so adjacency needs no positioning
maths and nothing can cover a board square or a clock at any width.

`role="status"` (polite), so a message is read when a screen reader pauses rather than
interrupting a move.

### Lifetime and replacement

| Rule | Value |
| --- | --- |
| Display lifetime | 4 seconds, measured from **arrival** |
| Two messages from the same seat | The newer **replaces** the older and restarts the timer |
| Maximum on screen | One per seat — at most two |

Keyed by side, so stacking is impossible by construction rather than by a cap. The
bubble is remounted on replacement, which is what makes a repeated message announce
again instead of appearing unchanged.

### Send flow

Selecting an item sends `game.quick_message.send` and shows **nothing**. The sender's
own bubble appears when the server's fan-out arrives, exactly as the opponent's does —
so one code path renders both, and there is no window in which an optimistic bubble and
a server echo are both on screen. The server is also authoritative about whether the
message was sent at all: a rate-limited or terminal-match send produces no bubble,
which an optimistic render would have got wrong.

A local ~600ms guard prevents a double-press producing two frames. **UX only** — it
cannot refuse anything the server would allow and is not a client-side limiter.

### Rate-limit and refusal feedback

A refusal is a small localized sentence beside the picker, never the generic transport
failure text. The server's own English prose is logged, never rendered.

`game.command.rejected` is shared with resign and the draw commands, so a refusal is
**attributed before it is rendered**: the game reducer applies one only while a command
is outstanding, and the quick-message hook claims `unknown_quick_message` outright and
`rate_limited` only while a send of its own is in flight. Before A64-023.2 the reducer
applied every such frame unconditionally, which would have shown a rate-limited quick
message as a refused draw offer.

### Terminal match

Once the match is terminal the picker's trigger is disabled and an open menu closes.
The **mute control stays enabled** — a player must still be able to silence a bubble
that is on screen. The backend refuses terminal sends regardless; the frontend reflects
that state rather than replacing the enforcement.

### Reconnect

Nothing reappears. Quick messages are never buffered by the gateway (§12), the client
holds no history, and bubbles live in component state that a remount starts empty. No
replay semantics were added for UI convenience.

---

## 11. Wire contract

Full protocol detail in [`websocket.md`](../docs/01-architecture/websocket.md) §20.

**Client → server**, `game` channel:

```json
{ "v": 1, "type": "game.quick_message.send", "channel": "game",
  "payload": { "match_id": "<uuid>", "message": "good_game" } }
```

**Server → both participants**, `game` channel, uncorrelated:

```json
{ "v": 1, "type": "game.quick_message.received", "channel": "game",
  "payload": { "match_id": "<uuid>", "from": "light",
               "message": "good_game", "sent_at": "<ISO-8601>" } }
```

`from` is a **side**, not a player id — a client knows which seat it holds, and a side is the
value it renders against.

**Refusals** reuse `game.command.rejected`, carrying the request's `request_id` and a
`code` from: `unknown_quick_message`, `not_in_room`, `not_a_participant`,
`match_not_active`, `rate_limited`, `internal_error`.

**There is no success acknowledgement.** The sender receives their own message through the
same fan-out, so one client code path renders a bubble whoever sent it — and a correlated
acknowledgement *plus* a broadcast would deliver the sender two frames for one message.

## 12. Delivery guarantees

| Property | Value | Why |
| --- | --- | --- |
| Sequence numbers | **No** | Nothing orders against it; it advances no ply |
| Resume buffer | **No** | S-5 — a non-ply entry breaks the resume contiguity check |
| Durable outbox event | **No** | No subscriber. An event nothing consumes is a queue that grows |
| Database persistence | **No** | S-3 |
| Delivery acknowledgement | **No** | Nothing acts on the answer; the sender cannot usefully retry a courtesy |
| Retry | **No** | A late "nice move" is worse than none |

**At most once, best effort.** A failed fan-out is counted and logged, never raised. Correct
game state never depends on any of this (G-4).

## 13. Observability

One counter, `gateway.quick_messages_total`, labelled by a bounded `outcome`: `sent`,
`rejected_invalid`, `rejected_not_in_room`, `rejected_not_participant`, `rejected_terminal`,
`rate_limited`, `internal`.

**No player, match, connection or message label.** The first three are unbounded; the fourth
is bounded and still absent, because "which messages get used" is a product question that
multiplies every series by six and nothing operational needs it.

The payload is **never logged**. It is untrusted input, and a log of rejected bodies would be
the free-text archive this feature exists not to have.

## 14. Acceptance criteria

- [x] **AC-1** — Given two participants in an `active` match, when one sends a catalogue
      entry, then both receive `game.quick_message.received` naming the sender's side, the
      identifier and the server's instant — and the sender receives no acknowledgement.
- [x] **AC-2** — Given any value that is not a catalogue member — free text, wrong casing, a
      near miss, an empty string, a non-string — when it is sent, then it is refused with
      `unknown_quick_message` and nothing is delivered to anybody.
- [x] **AC-3** — Given a sender who is a participant of two matches and joined to one, when
      they name the other, then it is refused and nobody in the other match receives it.
- [x] **AC-4** — Given a connection still in a match's room after the match completed, when
      it sends, then it is refused with `match_not_active`.
- [x] **AC-5** — Given a spectator of a live match, when a participant sends, then the
      spectator receives nothing; and when the spectator sends, they are refused.
- [x] **AC-6** — Given a connection whose quick-message budget is exhausted, when it sends
      and then submits a legal move, then the message is refused, the connection stays open,
      and the move is applied.
- [x] **AC-7** — Given quick messages sent around a move, when the replay buffer is read,
      then it holds the move and nothing else.
- [x] **AC-8** — Given every catalogue member, then each has a non-empty label in each of
      `uz`, `ru` and `en`.

## 15. Test coverage

| Test | Covers |
| --- | --- |
| `tests/unit/test_gateway_connection.py::TestQuickMessages` (7) | AC-1 … AC-7 |
| `tests/contract/test_quick_message_localisation.py` (3, one per locale) | AC-8 |
| `apps/web/src/features/game/quick-messages.test.tsx` (10) | The client experience — §10a |

## 16. Open questions

| # | Question | Blocked on |
| --- | --- | --- |
| OQ-1 | Do the six entries match real usage? | Telemetry after A64-023.2 ships the picker |
| OQ-2 | Should a rematch request be a catalogue entry or its own command? | It changes state, so probably a command — needs a product decision |
| OQ-3 | Is server-side block suppression needed, given BL-2 makes blocked pairings rare? | **Still open after A64-023.2** — the seam is `QuickMessageHandler._recipients_of` and was deliberately not wired; see §9.1 |
