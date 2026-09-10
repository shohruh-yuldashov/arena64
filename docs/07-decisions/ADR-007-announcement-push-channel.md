# ADR-007 — An administrative announcement may also be delivered as a push, chosen per broadcast

| Field | Value |
| --- | --- |
| **Status** | Accepted |
| **Date** | 2026-09-10 |
| **Deciders** | Shohruh |
| **Consulted** | — |
| **Supersedes** | [ADR-006](./ADR-006-administrative-broadcasts.md), **the channel clause only** — its Decision bullet "The channel is in-app only" and the matching Negative consequence. Every other decision in ADR-006 stands unchanged: stored text, the closed destination set, the absent `LOCKED` entry, and the refusal of email. |
| **Superseded by** | — |
| **Related** | `specs/admin.md` §7, `specs/notifications.md`, `app/modules/notifications/domain/broadcast.py`, `app/modules/notifications/domain/push.py`, `apps/web/pwa/push-presentation.ts` |

---

## Context

ADR-006 shipped administrative broadcasts as in-app only, and gave a good
reason for refusing email: provider cost, sender reputation, a bounce path,
and an unsubscribe obligation that the in-app preference switch already
discharges. That reasoning is about **email**. It was applied to push in the
same sentence, and push does not carry any of those costs — this platform
already runs a Web Push channel with VAPID keys, a delivery queue, a retry
policy and a preference gate, all built in A64-021.6.

The consequence in beta was that an operator could announce scheduled
maintenance and reach nobody who was not already looking at the tab. An
announcement is the one notification type a human decides to send, so the
question "is this worth interrupting somebody for" has already been answered
by a person with the role — which is precisely the judgement the push
allowlist exists to encode.

`BroadcastChannel` already existed with a single member, the column is a
`String(16)`, and `BroadcastResponse` and the audit record already carried
the channel. Nothing about the data model had to change to represent this.

## Decision

> We will let an administrator choose, per broadcast, whether an announcement
> is also delivered as a push, because the interruption decision belongs to
> the person composing the message and this platform already owns every
> mechanism required to honour it.

Specifically:

- `BroadcastChannel` gains `IN_APP_AND_PUSH` beside `IN_APP`. **Both write
  the in-app notification.** There is no member that pushes without storing,
  because a push on this platform carries an id and a type and the service
  worker fetches the record behind the reader's session — a push alone would
  be a buzz that leads nowhere.
- The default is `IN_APP`. A client that omits the field, including one that
  predates it, cannot start interrupting people.
- `PLATFORM_ANNOUNCEMENT` joins `PUSH_CAPABLE_TYPES`. Membership is necessary
  but not sufficient: the broadcast's channel decides whether any delivery
  row is written at all.
- **The push payload carries no authored text.** The service worker renders
  "Arena64 — You have a new announcement." from its compiled table, exactly
  as every other type does. ADR-006's stored-text exception stays scoped to
  the in-app row.
- The preference is unchanged and is asked twice: once by the expander before
  a row is written, once by `PushDeliveryService` at send time. An
  administrator cannot reach a player who muted the `announcement` category
  or the push channel.
- Email remains unoffered, for exactly the reasons ADR-006 gave.

## Options Considered

### Option 1 — A per-broadcast channel choice, generic push text *(chosen)*

**Summary:** The composer offers "also send a push"; the payload carries the
notification id and type, and the worker renders a fixed sentence.

| Pros | Cons |
| --- | --- |
| The interruption is a human decision, made per message | The lock screen says nothing about the announcement's subject |
| No authored text leaves the session, so no lock-screen disclosure and no injection surface | An operator may expect their title to appear and be surprised |
| Reuses the existing queue, retry, VAPID and preference path entirely | |
| Default is quiet, so the change cannot regress an existing deployment | |

### Option 2 — Push the announcement's title and body

**Summary:** Extend `PushPayload` with the operator's text so the lock screen
shows the real message.

| Pros | Cons |
| --- | --- |
| Far more useful at a glance | An announcement can say "your account has been restricted"; that is a disclosure on a surface the reader did not choose |
| One fewer tap | Reverses A64-021.6 §12's approach B for one type, and the next type would cite it |
| | Puts operator-authored text into a payload the browser stores outside the session |
| | Costs the 4 KB budget for text the app can render for free |

### Option 3 — Always push every announcement

**Summary:** Drop the channel field; treat announcements as inherently
interrupting.

| Pros | Cons |
| --- | --- |
| Nothing to choose, nothing to forget | A routine notice buzzes every phone on the platform |
| | Removes the judgement that earns this type its place on the allowlist |
| | No way to send a quiet announcement, which most announcements are |

### Option 4 — Do nothing

Announcements stay in-app only. Operators keep sending maintenance notices
that reach only players already looking at the tab, and the console keeps a
"Channel: In-app" row that reads as a choice and is not one. The gap between
what the console implies and what it does is itself the defect.

## Rationale

Three criteria decided it.

**Who owns the interruption.** The push allowlist's whole argument is that a
push is an interruption and each member must earn it. Every other member
earns it by category — the platform judged the *type* worth interrupting for.
An announcement earns it differently and better: a person with the role
judged *this message* worth it. That is a stronger warrant than any static
rule, and Option 3 throws it away.

**What may appear on a lock screen.** A64-021.6 §12 chose approach B on the
grounds that server-composed text is specific text, shown in public, to
whoever is looking. An announcement is the type most likely to be sensitive
and least likely to be expected, so it is the worst candidate for relaxing
that rule — Option 2 relaxes it for exactly the wrong one. Keeping the
generic sentence also means no operator string travels in a payload, which
makes injection unreachable rather than filtered.

**What a quiet default protects.** The admin console, the API and any
integration all keep working unchanged and silent. The feature can only make
noise where somebody asked for noise.

## Consequences

### Positive

- An operator can reach players who are not in the tab, which is the case a
  maintenance notice exists for.
- The console's "Channel" row becomes a real choice rather than a label.
- The audit record already stored `channel`, so choosing push is attributable
  with no change to the audit trail.
- `push_deliveries_for` is now shared by the writer and the expander, so
  "what this platform pushes" is one rule with one home rather than two.

### Negative

- A push says only that an announcement arrived. An operator who writes a
  careful title will not see it on the lock screen, and the accompanying help
  text in the console has to say so.
- Announcements are now capable of waking every registered browser on the
  platform. The bound is the operator's judgement plus the per-player
  preference; there is no platform-wide rate limit on broadcasts, and adding
  one is deliberately out of scope here.
- A deployment without a VAPID key silently delivers an `IN_APP_AND_PUSH`
  broadcast as in-app only. That is the correct behaviour — the announcement
  still arrives — but it is invisible to the operator who asked for a push.

### Neutral

- `BroadcastChannel` is a two-member enum whose members both include in-app.
  The name `IN_APP_AND_PUSH` is deliberately verbose so no reader has to
  learn that "push" silently also means "in-app".
- No migration. `notification_broadcast.channel` is a `String(16)` and
  `in_app_and_push` is fifteen characters.

## Impact

| Area | Impact |
| --- | --- |
| Architecture | `BroadcastExpander` gains a push-enqueue step inside its existing transaction. The push fan-out moves out of `DurableNotificationWriter` into `push_delivery_service.push_deliveries_for`, shared by both. |
| Data model | None. The `channel` column already exists and already stores a string; one new value fits the width. |
| Security | No authored text enters a push payload, so the lock-screen disclosure surface and the injection surface are both unchanged. The preference gate is asked twice, and an administrator cannot bypass either. |
| Operations | A process without a VAPID key delivers the in-app half and queues nothing; `broadcast_push_enqueued` logs counts and the broadcast id, never a recipient or an endpoint. |
| Developer workflow | A new `PUSH_CAPABLE_TYPES` member now requires a matching entry in `pwa/push-presentation.ts`, enforced by a test rather than by review. |

## Compliance & Enforcement

- `tests/unit/test_broadcast_push.py` — the channel decides, a muted player
  receives neither row nor push, a replayed batch enqueues nothing, and a
  process that cannot push queues nothing.
- `tests/unit/test_announcement_presentation.py` — the channel field is a
  closed enum defaulting to `in_app`, and the service worker's table names
  every pushable type and carries no authored text for this one.
- `apps/admin/src/pages/notifications-broadcast.test.tsx` — the request body
  is a pinned field set, the default is `in_app`, and the confirmation dialog
  restates the chosen channel.

## Follow-Up Actions

- [ ] Decide whether broadcasts need a platform-wide rate limit — Shohruh, before general availability
- [ ] Surface "push unavailable in this deployment" in the console rather than delivering quietly — Shohruh, unscheduled

## Revisit Criteria

- Players begin disabling the push channel at a materially higher rate after
  announcements start pushing.
- A product requirement appears for per-language announcements, which would
  change what a push could usefully say.
- An announcement is sent that should have shown its title on the lock
  screen, and the privacy argument above is judged not to apply to a
  subclass of announcements worth modelling separately.

## References

- [ADR-006](./ADR-006-administrative-broadcasts.md) — the decision this amends
- [ADR-003](./ADR-003-pwa-service-worker.md) — why this platform owns its service worker
- `app/modules/notifications/domain/push.py` — the allowlist and the payload's contents
- RFC 8291 — Web Push message encryption
