# Chat

> **Status:** **Deferred — superseded for in-match communication.** Not built, not planned.
> **Owner:** _Unassigned_
> **Decision:** [`ADR-004`](../docs/07-decisions/ADR-004-quick-messages-not-free-text-chat.md)
> **See instead:** [`specs/quick-messages.md`](./quick-messages.md)

## Status

**Arena64 has no free-text chat, and this file is not a queue of work.** It records that the
decision was made deliberately rather than left pending.

In-match communication ships as **quick messages** — a fixed, server-owned catalogue of
semantic identifiers, delivered between the two players of one live match and stored nowhere.
That is specified in [`quick-messages.md`](./quick-messages.md) and is *not* a first step
toward this document: it is what replaced it.

## Why free text was ruled out

Summarised from `ADR-004`, which holds the full reasoning:

| # | Constraint |
| --- | --- |
| 1 | Free text needs reactive moderation. `admin` is unbuilt, unowned, and not on the roadmap — shipping prose first means shipping an abuse surface with no response to abuse |
| 2 | The moderation archive is a permanent retention obligation over personal data, held for a process that does not exist |
| 3 | Players do not share a language. Arena64's audience reads Uzbek, Russian and English; prose between two of them is noise |
| 4 | What players actually want to say during a draughts game is a short, closed set of courtesies — and a set that can be enumerated does not need to be moderated |

The design that *was* specified for this — `ChatThread` with four scopes, `Message`,
CT-1 … CT-6 — is retained in `docs/01-architecture/domain-model.md` §9.1–§9.2, marked
superseded. Its rules were not discarded: CT-1 (a completed match accepts no further messages)
and CT-2 (spectators are not party to the players' conversation) are enforced today as QM-3 and
QM-4.

## What would reopen this

`ADR-004`'s revisit criteria, principally: **a moderation capability shipping** — `admin` with
a reports queue, a sanctions model and human reviewers. Free text is defensible then and is not
before.

Note that a future scope this design would serve — direct messages between friends, for
instance — does not automatically mean prose. It may still be a catalogue.

## Scopes that remain unaddressed

Out of scope today, and each would need its own decision rather than inheriting one:

- Direct messages between friends
- Lobby, global, or tournament-wide communication
- Spectator communication (explicitly excluded — `ADR-004`)
