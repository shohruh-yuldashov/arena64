"""`tournament`'s published surface.

Deliberately **minimal**: the vocabulary a consumer needs to name a
tournament, and — since A64-019.5H — one command it can act with.
Registration, brackets and standings are published when the phases that
build them do; publishing a type before its use case exists is how a
surface accumulates things nothing supports.

`notifications.py` — `TournamentNotificationReader` and its two values.
    Two reads, for A64-021.4's fan-out: who is in a tournament and what it
    is called, and what everybody finally placed. No write, no bracket, no
    schedule — see that module on why the surface is that narrow.

`events.py` is not a module: the four events a consumer subscribes to are
    re-exported below, because an outbox consumer keys on `event_type` and
    building that set from strings is how a renamed event silently stops
    matching.

`attendance.py` — `TournamentAttendance`. The gateway telling a tournament
    that a player reached one of its matches, which is what replaced the
    acceptance handshake when tournament matches became system-activated
    (§6e). One method, recording a fact and returning nothing that can act
    on a bracket.

Everything else is private, held by `tournament-internals-are-private`.
"""

from app.modules.tournament.domain.events import (
    PlayerRegistered,
    RoundPublished,
    TournamentCompleted,
)
from app.modules.tournament.domain.tournament import (
    TournamentFormat,
    TournamentStatus,
)
from app.modules.tournament.public.attendance import TournamentAttendance
from app.modules.tournament.public.notifications import (
    TournamentAudience,
    TournamentNotificationReader,
    TournamentResults,
)

__all__ = [
    "PlayerRegistered",
    "RoundPublished",
    "TournamentAttendance",
    "TournamentAudience",
    "TournamentCompleted",
    "TournamentFormat",
    "TournamentNotificationReader",
    "TournamentResults",
    "TournamentStatus",
]
