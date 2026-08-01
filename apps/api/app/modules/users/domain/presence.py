"""`Presence` — whether a player is here right now, and when they last were.

Framework-free like the rest of `domain/` (architecture.md §8). No clock:
this is a *reading*, and the instant it describes arrives with it.

## Why this lives in `users` and not in `profiles`

domain-model.md §299 assigns `Presence` to the `users` module and Redis as
its store, and `profiles.domain.profile.PublicProfile` has said so in prose
since A64-012.1 ("presence is `users`-owned and lives in Redis with a TTL").
`profiles` is a *composition* — it renders a player, it does not own any
fact about one — so a presence type declared there would make the platform's
public read endpoint the owner of a concept `friends`, `matchmaking` and the
gateway all need next.

The store is Redis and the module is `users`; those are independent choices
and domain-model.md DM-04 is explicit that the second does not follow from
the first. `users` reaches Redis through
`users.public.PresenceProvider` exactly as it reaches PostgreSQL through
`UserRepository` — a port in the published surface, an adapter in
`infrastructure/`.

## Why this is ephemeral state rather than domain data

DM-04: "Presence, connections, spectator subscriptions and rate-limit
counters are true facts about right now. They are not part of the persistent
domain and appear in this model only so that nobody later *promotes* them to
entities."

Two consequences run through everything below. Nothing here has an
identifier, because there is nothing to refer to it by later; and nothing
here is durable, because the record is gone the moment its TTL lapses and
that is the *design*, not a limitation to be worked around. A `last_seen`
that outlived the presence window would be a behavioural history of a person
kept in a store configured for speed — see `PrivacySettings.show_last_seen`
on why that field is the one privacy default that is off.

## Why "offline" and "never recorded" are different values

`is_online=False` means the platform observed this player *leaving*: a
socket closed cleanly and the gateway wrote a record saying so, which is
what makes "last seen four minutes ago" a thing a profile can say.

The absence of a record entirely — the provider returning `None` — means the
platform knows nothing: the window expired, presence has never been written
for this account, or the store was unreachable. Those three are
indistinguishable *by construction* here, and
`profiles.presentation.schemas` keeps them indistinguishable on the wire by
rendering all of them, and a player who has hidden their presence, as
`null`.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID


class DeviceType(StrEnum):
    """What a player is connected from.

    **Recorded, never published.** No response schema on the platform
    carries this, and A64-012.7 is explicit that only `is_online` and
    `last_seen` are exposed. It is here because the gateway knows it at
    connect time and the record is the contract with the gateway — adding a
    field to a live keyspace later means every key written before the change
    decodes short.

    A closed set for the reason `BoardTheme` is one: an open string column
    accumulates spellings from every client version ever shipped. Unlike
    `BoardTheme`, decoding is deliberately **tolerant** — a value this build
    does not recognise reads as `None` rather than raising, because a newer
    gateway node writing a fourth device type must not break profile reads on
    an older API node during a rolling deploy.
    """

    WEB = "web"
    MOBILE = "mobile"
    DESKTOP = "desktop"


@dataclass(frozen=True, slots=True)
class Presence:
    """One player's presence, as last observed.

    Frozen: a reading, not an accumulator. Whatever maintains presence owns
    its own write path; what crosses a boundary is a snapshot.

    Constructed only by an adapter decoding a stored record, and by tests.
    There is deliberately no validation in `__post_init__` — unlike
    `PlayerStatistics`, which checks an arithmetic invariant, there is no
    combination of these four fields that is internally contradictory. A
    record that *is* malformed never reaches this type: the adapter treats
    an undecodable value as no record at all, which is the fail-safe
    direction.
    """

    is_online: bool
    """Whether a socket was open at the moment this record was written.

    Read together with the TTL rather than on its own: `False` here means
    the platform saw this player disconnect and the record has not yet
    expired, so `last_seen` beside it is recent and meaningful. Once the
    window lapses there is no record and therefore no `Presence` at all.
    """

    last_seen: datetime
    """When the player was last observed, timezone-aware UTC (DM-14).

    Not nullable, and that is the point of pairing it with the record rather
    than storing it separately: a presence record exists *because* something
    observed this player, so there is always an instant to report. Absence of
    an observation is the absence of the whole record.
    """

    session_id: str | None = None
    """The gateway session behind the observation — **never published.**

    A64-012.7: "never expose internal session identifiers." Nothing maps this
    onto a response schema, and the two schemas that render presence
    (`ProfileResponse`, `MyProfileResponse`) have no field it could land in.

    Carried because the future readers of presence need it and the keyspace
    has to have room for it from the first release: a live challenge is
    delivered to a *connection*, not to an account, and the value that routes
    it is written by the same gateway that writes this record.
    """

    device_type: DeviceType | None = None
    """What the player was connected from — **never published**, for the same
    reason `session_id` is not.

    `None` when the recorder did not say, or when the stored value is one
    this build does not know. See `DeviceType`.
    """


@dataclass(frozen=True, slots=True)
class LapsedPresence:
    """A player whose presence window closed without anybody observing it —
    A64-013.8.

    The sweeper's unit of work. Two fields and no more, because that is all
    a missed `offline` transition is: who left, and when their record was due
    to stop being true.

    `lapsed_at` is **the expiry instant, not the sweep instant**, and the
    difference matters to whoever reads the notification: a sweeper running
    on a thirty-second tick would otherwise report every departure as having
    happened at a tick boundary, and "went offline 12:00:30" for somebody who
    left at 12:00:03 is a fabrication the roster can easily avoid — it is
    scoring exactly that instant.
    """

    player_id: UUID
    lapsed_at: datetime
