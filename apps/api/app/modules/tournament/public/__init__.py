"""`tournament`'s published surface.

Deliberately **minimal**: the vocabulary a consumer needs to name a
tournament, and — since A64-019.5H — one command it can act with.
Registration, brackets and standings are published when the phases that
build them do; publishing a type before its use case exists is how a
surface accumulates things nothing supports.

`attendance.py` — `TournamentAttendance`. The gateway telling a tournament
    that a player reached one of its matches, which is what replaced the
    acceptance handshake when tournament matches became system-activated
    (§6e). One method, recording a fact and returning nothing that can act
    on a bracket.

Everything else is private, held by `tournament-internals-are-private`.
"""

from app.modules.tournament.domain.tournament import (
    TournamentFormat,
    TournamentStatus,
)
from app.modules.tournament.public.attendance import TournamentAttendance

__all__ = ["TournamentAttendance", "TournamentFormat", "TournamentStatus"]
