# Feature Specification — Match History and Replay

| Field | Value |
| --- | --- |
| **Spec ID** | `SPEC-REPLAY` |
| **Status** | Approved for v0.6.0 |
| **Owner** | _Unassigned_ |
| **Created** | 2026-08-05 |
| **Last updated** | 2026-08-05 |
| **Related ADRs** | [`ADR-001`](../docs/07-decisions/ADR-001-glicko2-incremental.md) |
| **Related specs** | [`game-engine.md`](./game-engine.md), [`rating.md`](./rating.md), [`live-game/audit.md`](./live-game/audit.md) |

---

## 1. Summary

Two read-only capabilities over finished games:

| | |
| --- | --- |
| **Match history** | A player's finished matches — opponent, result, when, rating change |
| **Match replay** | One match played back ply by ply, from the durable move log |

Both are read-only over cold data. Neither writes, and neither can affect a live game.

## 2. Scope — v0.6.0

| In | Out, and why |
| --- | --- |
| (a) Match history list | |
| (b) Ply-by-ply replay | |
| | (c) **PDN notation and export** — deferred. The dialect is an undecided rules question; see §8 |
| | (d) **Analysis playback** — needs engine evaluation, which does not exist |

`architecture.md` §11 lists all four under the `replay` module. Two ship; the other two are
deferred by product decision, not overlooked.

## 3. Privacy

| Match kind | Visible to |
| --- | --- |
| **Rated** | Everyone |
| **Casual** | **Participants only** |

**No configurable privacy in this version.** A player cannot hide a rated match and cannot
publish a casual one.

The rule follows what the platform already asserts: `profiles`' privacy settings explicitly do
*not* cover ratings, because a rating is a public competitive claim — and a rated match is the
evidence for one. A casual game makes no such claim, so it stays between the two people who
played it.

**Enforced on the read, never by omitting a filter.** A history query names the viewer; a
replay checks the match before returning a single ply. A casual match requested by a stranger is
refused with the same answer an unknown match gets, so match ids stay unenumerable — the rule
`MatchRosterReader` and the spectator policy already keep.

## 4. Engine versions — replay refuses, history does not

`SUPPORTED_ENGINE_VERSIONS` holds version 2 only. AD-15 records the version every match was
played under, and §11's rule is that replay **refuses rather than approximates**: a game played
under rules that have since been fixed must not be reconstructed under the new ones, because the
reconstruction could end differently from the game that was actually rated and displayed.

| Surface | Behaviour for an unsupported version |
| --- | --- |
| History | **Visible.** The metadata — opponent, result, date, rating change — is stored, not derived, so nothing about it depends on the engine |
| Replay | **Refused**, with a documented `unsupported_engine_version` response. **No attempt is made** |

The distinction matters: hiding the match would make a player's record incomplete over an
engineering detail, and replaying it anyway would make the archive disagree with history.

## 5. Retention

**Append-only. No deletion policy in this version.**

`specs/live-game/audit.md` §6 records the consequence: `game.match` and `game.move` grow with
every game ever played, bounded by nothing. That is correct — a played game is history, and the
move log is what makes it replayable — and it is stated here because this is the feature that
reads it.

The first move when the size matters is monthly range partitioning on `created_at`; the access
pattern is "one player's recent matches" and "one match's moves", never a date range across
players.

## 6. Architecture

**Replay is published through `game.public`.** `game.domain` is not exposed.

`ReplayEngine` already exists in `game.domain.replay` and reconstructs a match by playing every
ply through the same validator, applier, terminal evaluator and draw rules a live game uses. It
stays there: it is the rules, and the rules belong to the module that owns them (R-2 permits
`game`, `replay` and `fairplay` to import `engine`; only `game` may *write* with it).

What `replay` gets is a published read. It never holds a `Match`, a `Position` or a
`MoveRecord` — the same boundary the gateway keeps, and for the same reason.

```
replay  ->  game.public  ->  game.application  ->  ReplayEngine  ->  engine
   |
   +--> the privacy rule, the HTTP surface, pagination
```

**Reachability.** Every background entry point this epic adds is asserted to be named by a
composition root — `tests/unit/test_reachability.py`, added in A64-018.1. Two consecutive epics
shipped complete, tested, unreachable components; that check is the answer.

## 7. Non-functional

| Property | Rule |
| --- | --- |
| Pagination | Keyset, like the leaderboard. `OFFSET` shifts when rows are inserted between reads |
| Ordering | Newest first, tie-broken by match id, so the order is total |
| Replay cost | One indexed read of the move log plus one engine application per ply. Linear in the game, bounded by the draw rules |
| Caching | **None.** A finished match is immutable, so the correct cache is HTTP's — not a second copy of an append-only record |

## 8. Open questions

| # | Question | Blocked work |
| --- | --- | --- |
| OQ-1 | Which PDN dialect, and how are multi-jump paths written? | (c) notation and export. DM-09 warns that two capture sequences can share a short notation, so this is a rules decision rather than a formatting one |
| OQ-2 | Is engine evaluation in scope at all? | (d) analysis playback |
| OQ-3 | Should a player be able to hide a rated match? | Configurable privacy — §3 says no in this version |
| OQ-4 | When does the move log need partitioning? | §5 — a measurement, not a decision |
