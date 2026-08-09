# ADR-004 — Arena64 has no free-text chat; in-match communication is a predefined catalogue

| Field | Value |
| --- | --- |
| **Status** | Accepted |
| **Date** | 2026-08-09 |
| **Deciders** | Shohruh |
| **Consulted** | — |
| **Supersedes** | The free-text chat design in `docs/01-architecture/domain-model.md` §9.1–§9.2 (`ChatThread`, `Message`) |
| **Superseded by** | — |
| **Related** | `specs/quick-messages.md`, `specs/chat.md`, `docs/01-architecture/websocket.md` §20, `docs/01-architecture/architecture.md` §7 |

---

## Context

A64-023 gives players a way to communicate during a live match. Every prior architecture
document on this platform assumed that meant **chat**: `domain-model.md` §9.1 specified a
`ChatThread` aggregate with four scopes and a `Message` entity persisted in PostgreSQL,
`architecture.md` §7 drew a `GW --> chat` command edge, `repositories.md` §250 listed a
`ChatThreadRepository`, and `services.md` §8.5 wrote a redaction rule for message bodies.
None of it was built. `specs/chat.md` has been a placeholder since the repository was
created.

That design is coherent and it is the design of a different product. Four constraints made
it the wrong one to build here:

1. **Free text needs moderation, and there is none.** CT-5's redaction, CT-6's log
   exclusion and the reporting flow all assume an `admin` module with a moderation queue,
   a sanctions model and human reviewers. `admin` is unbuilt, has no owner, and is not on
   the roadmap. Shipping free text before the thing that adjudicates it means shipping an
   abuse surface with no response to abuse — and a competitive board game is a context
   where the abuse is the *point* of the message, not incidental to it.

2. **The retention obligation is permanent.** CT-5 requires proving that a message existed
   and was removed, which means every message is a row that outlives the match. That is
   storage growing in proportion to how much players talk, held for a moderation process
   that does not exist, containing personal data under `database.md` §14.1.

3. **The audience is multilingual.** Arena64 supports Uzbek, Russian and English
   (`core.enums.Locale`), and the primary audience reads Uzbek. Free text between two
   players who do not share a language is not communication; it is noise that a translation
   feature would then have to be built for.

4. **Nothing in the product needs prose.** What players actually want to say during a
   draughts game is a short, closed set of courtesies — the same set on every board game
   platform. That set can be enumerated.

## Decision

> We will not implement free-text chat. In-match communication is a **fixed catalogue of
> semantic identifiers owned by the server**, carried over the existing game realtime
> infrastructure, delivered only between the participants of one live match, and never
> persisted — because a catalogue in which no abusive message exists is a stronger
> guarantee than moderating one after it has been sent, and it is the only version of this
> feature that can ship before a moderation capability does.

Concretely, and each clause is testable:

- The wire carries a member of `app.gateway.quick_messages.QuickMessage`. **No frame on
  this platform carries user-authored text**, and no frame has a field one could occupy.
- The catalogue is server-authoritative. An identifier the server does not recognise is
  refused with `unknown_quick_message`.
- Every catalogue entry is positive or neutral **by construction**. A taunt cannot be sent
  because no taunt exists to send.
- The **receiving** client renders the identifier in its own locale. The server never
  sends display text.
- Delivery is scoped to the two seats of one `active` match, derived server-side from
  `game.public.MatchRoster`. There is no recipient field on the frame.
- Nothing is written to PostgreSQL, to the replay buffer, or to match history.
- This is **not** the `chat` bounded context. `app/modules/chat/` is not created.

## Options Considered

### Option 1 — A predefined catalogue, gateway-owned and ephemeral *(chosen)*

**Summary:** A closed enum of six identifiers, sent over the existing socket through a new
frame type, authorised against the live match roster, fanned out to the two participants,
and stored nowhere.

| Pros | Cons |
| --- | --- |
| Abuse is prevented rather than moderated — there is no harmful message to send | Players cannot say anything the catalogue does not anticipate |
| Ships with no moderation capability, no reports queue and no sanctions model | A catalogue gap pushes players toward off-platform contact |
| Localisation is free: an identifier renders in the reader's language | Adding an entry is a release, not a configuration change |
| No storage growth, no personal data, no retention obligation | Nothing can be quoted in a future dispute |
| Reuses the whole existing realtime tier — one frame type, one handler | The gateway gains a product policy, which R-7 constrains (see Rationale) |

### Option 2 — Build the documented `chat` module now

**Summary:** Create `app/modules/chat/` with `ChatThread`, `Message`, a repository and the
`GW --> chat` command edge, restricted at first to predefined messages.

| Pros | Cons |
| --- | --- |
| Honours the drawn architecture without divergence | Six packages and a migration to own one frozen tuple of six strings |
| A later free-text decision would need no restructuring | Speculative generality — CLAUDE.md §1.7 and §3.5: shared structure is earned by a second consumer |
| Message history would exist if a dispute needed it | Persisting an ephemeral courtesy creates a retention and erasure obligation with no reader |
| | An aggregate whose only invariant is "the identifier is in an enum" is not an aggregate |

### Option 3 — Free-text chat with a filter word list

**Summary:** Ship prose, and block abuse with a denylist.

| Pros | Cons |
| --- | --- |
| Players can say anything | A denylist is trivially defeated by spelling, and blocks innocent words in three languages |
| Familiar to players from other platforms | `ChatFilterRule` is listed in `domain-model.md` §1399 as explicitly *deferred* — "all moderation is reactive" |
| | Requires the moderation capability that does not exist, on day one |

### Option 4 — Do nothing

**Summary:** Players communicate through moves and resignations only.

| Pros | Cons |
| --- | --- |
| Zero surface, zero cost | Players read silence as rudeness; "good game" is a norm of the game itself |
| No decision to revisit | Players route around it — usernames become messages, or contact moves off-platform where nothing can be enforced |

## Rationale

The criterion that decided it was **what happens when the feature is misused**, evaluated
against a platform that has no moderation capability and no plan to build one this year.

Option 3 fails immediately: it needs reactive moderation on the day it ships. Option 2
fails a different test — it is not *wrong*, it is premature, and it converts a decision
that is currently one enum into a schema, a migration and a retention policy that must be
justified to a reader who asks what consumes the rows. Option 4 loses to observed
behaviour: players on every board game platform say "gg", and a product that forbids it
gets it anyway in a worse channel.

Option 1 wins because it moves the guarantee from *enforcement* to *construction*. There is
no filter to defeat, no queue to staff, and no report to adjudicate, because the set of
sendable messages contains nothing worth reporting. That property is what makes it
shippable now.

**On the gateway owning it, against R-7 ("the gateway contains no domain logic").** A quick
message changes no match state, produces no event, and is derived from nothing durable — it
is a routing decision about who may receive a frame in one live match. That is the same
category as `SPECTATOR_SAFE_EVENTS`, `SPECTATABLE_STATES` and `BlockAwareSpectatorPolicy`,
all of which are transport-scope policy the gateway already owns by precedent (A64-016.7).
The line R-7 draws is held: the gateway holds exactly one `game` capability here — a single
`MatchRosterReader` read — and cannot advance, settle, or alter a match.

## Consequences

### Positive

- No moderation capability is required to ship in-match communication.
- No personal data, no retention obligation, no erasure path, and no storage that grows
  with how much players talk.
- Localisation is structural: a fourth locale is three JSON files and no server change.
- The catalogue can gain and lose entries without a protocol change.

### Negative

- Players cannot express anything outside the catalogue, including legitimate things — an
  apology for a disconnection, a rematch request, a clock query.
- No message history exists, so a dispute about *conduct* cannot be adjudicated from
  transcripts. Conduct disputes about **gameplay** are unaffected: the move log is durable.
- A message sent while the opponent's socket is down is lost with no trace.

### Neutral

- `specs/chat.md` stays a placeholder, now explicitly deferred rather than pending.
- The `admin` module's future scope shrinks: there are no chat reports to triage.

## Impact

| Area | Impact |
| --- | --- |
| Architecture | `GW --> chat` is not built; the gateway owns quick messages as transport-scope policy. `app/modules/chat/` is not created. |
| Data model | None. No table, no migration, no ORM model, no outbox event. `ChatThread` and `Message` are removed from the current-architecture entity set and marked deferred. |
| Security | Arbitrary text cannot be transported. Sender identity is the socket's redeemed ticket; recipients are derived from the roster. A terminal match refuses new messages (the rule CT-1 stated). |
| Operations | One counter, `gateway.quick_messages_total`, with a bounded `outcome` label. Two Redis rate-limit rules per connection. No new store, no new worker. |
| Developer workflow | Adding a catalogue entry is one enum member plus three translations, held together by `tests/contract/test_quick_message_localisation.py`. |

## Compliance & Enforcement

| Property | Enforced by |
| --- | --- |
| No user text on the wire | `parse_quick_message` — anything that is not a catalogue member returns `None`; asserted against free text, wrong casing, near misses, empty strings and non-strings |
| No `app/modules/chat/` | Its absence; `.importlinter` gains a contract the day one is created |
| The gateway reaches `game` only through `public` | `.importlinter` contract *the gateway reaches every module only through its published surface* |
| Never persisted, never replayed | `test_a_quick_message_is_never_written_to_the_replay_buffer`; the handler holds no repository and no event buffer |
| Spectators excluded | `test_a_spectator_can_neither_send_nor_receive`; absent from `SPECTATOR_SAFE_EVENTS`, and the handler passes no audience |
| Every entry renders in every locale | `tests/contract/test_quick_message_localisation.py` |

## Follow-Up Actions

- [ ] A64-023.2 — recipient-side mute and preferences, at the seam `specs/quick-messages.md` §7 names.
- [ ] A64-023.3 — duplicate suppression and the remainder of the abuse model.
- [ ] Revisit the catalogue's contents once real usage exists — owner: product.

## Revisit Criteria

Reopen this decision when **any** of the following becomes true:

1. A moderation capability ships — `admin` with a reports queue, a sanctions model and
   reviewers. Free text is defensible then and is not before.
2. Measured evidence that players are routing around the catalogue — a rise in usernames
   used as messages, or in off-platform contact requests.
3. A product decision to support a scope this design cannot serve: direct messages between
   friends, tournament-wide announcements, or spectator conversation. None of those are
   in-match participant communication, and none should be bolted onto this frame.

Note that (3) does **not** automatically mean free text. A direct-message feature may still
be a catalogue; the decision to reopen is about scope, not about prose.

## References

- `specs/quick-messages.md` — the feature specification this record constrains
- `docs/01-architecture/domain-model.md` §9.1 — the superseded design, retained with a note
- `docs/01-architecture/websocket.md` §20 — the transport contract
- `docs/01-architecture/architecture.md` AD-10, AD-11 — channel separation and multiplexing
