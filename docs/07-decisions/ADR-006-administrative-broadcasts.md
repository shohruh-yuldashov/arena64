# ADR-006 — Administrative broadcasts store their text and keep the closed destination set

| Field | Value |
| --- | --- |
| **Status** | Accepted |
| **Date** | 2026-09-05 |
| **Deciders** | Shohruh |
| **Consulted** | — |
| **Supersedes** | — |
| **Superseded by** | — |
| **Related** | `specs/admin.md` §7, `specs/notifications.md`, `app/modules/notifications/domain/broadcast.py`, `app/modules/notifications/domain/record.py` |

---

## Context

A64-027A required the console to let an administrator send a notification to
players. Nothing on the platform could do this, and the absence was
deliberate: `notifications.public.administration` states that through the
admin port there is "no way to create a notification, choose a recipient,
choose a type, choose a payload or choose a destination", and that is
precisely what made the admin surface safe to expose.

Three existing rules stood in the way, and each exists for a good reason.

**No rendered text is stored.** `NotificationType` is the contract: the
backend states a fact — `tournament_round_published` — and the client
renders it in uz, ru or en. Storing a sentence would freeze one language
into a row that outlives the build that wrote it.

**No URL is stored.** `NavigationTargetType` is a closed set of internal
destinations carrying at most one identifier, because "an event-supplied URL
would be an open redirect written into a table" and a pre-rendered path would
bake one build's routing into permanent rows.

**A player is entitled to silence.** `preference.LOCKED` contains exactly
one pair — `(SYSTEM, IN_APP)` — reserved for an account or security matter.
Everything else is mutable.

An announcement is prose an administrator writes. There is no translation
key for a sentence nobody has seen yet, so the first rule cannot be
satisfied as written. The question was which rule to bend, and how far.

## Decision

> We will store an administrative announcement's text verbatim as a new
> `PLATFORM_ANNOUNCEMENT` notification type, and we will keep the closed
> destination set and the preference gate untouched.

Concretely:

- A new `NotificationCategory.ANNOUNCEMENT`, **absent from `LOCKED`**, so a
  player who mutes it receives nothing.
- A new `NotificationType.PLATFORM_ANNOUNCEMENT` whose payload is
  `AnnouncementSummary(title, body, locale)` — plain text, no markup.
- Its `NavigationTarget` is always `HOME` with no `ref`. An administrator
  supplies no URL and cannot.
- The channel is in-app only.
- Delivery is a persisted `Broadcast` aggregate expanded by a worker in
  bounded batches, never inside the HTTP request.

The stored-text exception is scoped to one type and stated in that type's
own docstring, so a future author adding a second one has to decide
deliberately rather than by precedent.

## Options Considered

### 1. Reuse `SYSTEM` and its locked preference

Rejected. `SYSTEM` is locked *on* for in-app delivery, so filing broadcasts
under it would let an administrator reach every muted inbox on the platform
by choosing a dropdown value. That is a preference bypass dressed as reuse,
and it would also silently change what `SYSTEM` means for the preference
screen a player already sees.

### 2. A fixed catalogue of announcement templates with translation keys

Rejected for now, and it is the option that best preserves the existing
rules: `maintenance_scheduled`, `tournament_announced` and so on would each
be a real `NotificationType` rendered in the reader's own language.

It fails the actual requirement. An operator needs to say *when* the
maintenance is and *which* tournament, which means parameters; parameters
that carry a date and a name are most of the way to stored text, and the
catalogue still cannot express anything nobody anticipated. A template
system that covers half the cases and blocks the other half is worse than
one honest exception. Templates remain deferred (§21) as a convenience on
top of this, not a replacement for it.

### 3. Store text and allow an administrator-supplied action URL

Rejected. It is the natural next request and it is an open redirect written
into every inbox on the platform, reachable from a form. Richer targeting
needs a picker that resolves a real entity server-side; that is deferred
explicitly in `NavigationTargetType.HOME`'s docstring rather than
approximated with a text field.

### 4. Deliver synchronously in the admin request

Rejected. `O(accounts)` writes inside one HTTP request holds a connection
for the length of a delivery, loses everything on a restart, and makes the
response time a function of the platform's size.

## Consequences

**Positive.** The console gains the capability the product needed. The
open-redirect protection, the preference gate and the exactly-once delivery
guarantee are all unchanged — the broadcast reuses the notification table's
existing `(recipient_id, source_event_id, type)` unique key, so a replayed
worker batch writes nothing and needs no lease.

**Negative.** One notification type is not translatable, and a player whose
language differs from the administrator's reads it as written. The payload
carries `locale` so a client can mark the text up for a screen reader, which
mitigates the accessibility half and not the comprehension half. A platform
serving three languages will eventually want per-language announcements;
that is a future extension of this type, not a reversal of it.

**Negative.** Announcements are in-app only. Email and push exist on this
platform and neither is offered, because a broadcast over email is a
different risk class — provider cost, sender reputation, a bounce path and
an unsubscribe obligation that the in-app preference switch already
discharges.

## Compliance

- `AnnouncementSummary` is the only payload with stored prose; every other
  type still carries facts.
- No `url`, `html`, `markdown` or `image` field exists on the request
  schema, and `extra="forbid"` refuses one added to a form first. A contract
  test asserts this by posting each field name and expecting `422`.
- `ANNOUNCEMENT` must never be added to `preference.LOCKED`. A contract test
  asserts that a player who muted the category receives nothing.
- Broadcasting is guarded by `CurrentAdmin` and recorded in the audit trail
  with the audience *category* and never a recipient.
