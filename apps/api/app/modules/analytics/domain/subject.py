"""The analytics subject — how a person appears in the event store.

## Not `PlayerId`, and that is the whole design

The obvious implementation stores `PlayerId` on every row. It is already
opaque (DM-06), already survives erasure (AC-5), and already crosses every
context boundary. It would also make the raw event store **joinable to the
product database by primary key**, so an erased account's behavioural
history would remain attached to an identifier that every other table still
uses.

A64-027.2's D3 decision forbids exactly that: "simply keeping `PlayerId`, if
it can be re-linked to the user from another system, is not anonymisation."

So an analytics row carries a `subject_key` — a **random** value with no
derivation from anything. One table, `analytics.subject`, maps a player to
theirs, and that table is the only link that exists anywhere.

    analytics.subject   player_id -> subject_key      the one linkage
    analytics.event     subject_key                   never player_id

Erasure deletes the mapping row. The key is random rather than derived, so
there is nothing to recompute it from — not a hash to brute-force over a
small identifier space, not a ciphertext to decrypt with a key somebody
kept. What remains is a column of random values that group a person's events
together and identify nobody.

That is why aggregate correctness survives erasure: a cohort's retention
does not shift when a member leaves, because their rows still count as one
subject. They simply stop being *that* person's rows.
"""

from typing import NewType
from uuid import UUID

#: A subject's opaque analytics identity. A `UUID` at the type level and a
#: **random** one by construction — `NewType` rather than an alias so a
#: `PlayerId` cannot be passed where this is expected, which is the mistake
#: this module exists to prevent.
SubjectKey = NewType("SubjectKey", UUID)
