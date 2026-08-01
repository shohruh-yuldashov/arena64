"""Primitive projections of the `game` aggregate's own values.

Framework-free (architecture.md §8). Pure functions, no I/O, and no
Pydantic or ORM types anywhere near the domain.

The kernel's values — coordinates, pieces, boards, positions, moves,
engine versions — are projected by `engine.serialization`, and this module
calls it rather than re-encoding them. One encoding, reachable from
`replay` and `fairplay` (which may import `engine` and not `game`), and
extended here with the shapes only a match has.

## No hidden defaults

Every `*_from_primitive` requires every field. A record written by an older
build that lacked one must fail loudly rather than be reinterpreted: a
promotion flag that quietly defaulted to `null` would replay into a
different position, which is the failure AD-15 exists to surface rather
than absorb.

The one optional pair is deliberate and explicit — `think_time_ms` and
`remaining_clock_ms` are `None` on every record this build writes, because
there are no clocks yet. They are still required *keys*; `None` says "not
measured", and a missing key says nothing at all.
"""

from collections.abc import Mapping, Sequence
from typing import Any

from app.modules.engine import BoardVariant, PlayerSide
from app.modules.engine.serialization import (
    engine_version_from_primitive,
    engine_version_to_primitive,
    move_from_primitive,
    move_to_primitive,
    position_from_primitive,
    position_to_primitive,
)
from app.modules.game.domain.move_log import MoveRecord
from app.modules.game.domain.replay import ReplayData
from app.modules.game.domain.result import MatchOutcome, MatchResult, TerminationReason

Primitive = Mapping[str, Any]


def move_record_to_primitive(record: MoveRecord) -> dict[str, Any]:
    return {
        "ply_number": record.ply_number,
        "move": move_to_primitive(record.move),
        "resulting_position_hash": record.resulting_position_hash,
        "think_time_ms": record.think_time_ms,
        "remaining_clock_ms": record.remaining_clock_ms,
    }


def move_record_from_primitive(entry: Primitive) -> MoveRecord:
    return MoveRecord(
        ply_number=entry["ply_number"],
        move=move_from_primitive(entry["move"]),
        resulting_position_hash=entry["resulting_position_hash"],
        think_time_ms=entry["think_time_ms"],
        remaining_clock_ms=entry["remaining_clock_ms"],
    )


def match_result_to_primitive(result: MatchResult) -> dict[str, Any]:
    return {
        "outcome": result.outcome.value,
        "reason": result.reason.value,
        "winner": None if result.winner is None else result.winner.value,
    }


def match_result_from_primitive(entry: Primitive) -> MatchResult:
    winner = entry["winner"]
    return MatchResult(
        outcome=MatchOutcome(entry["outcome"]),
        reason=TerminationReason(entry["reason"]),
        winner=None if winner is None else PlayerSide(winner),
    )


def replay_to_primitive(replay: ReplayData) -> dict[str, Any]:
    """A whole replay payload, in declaration order.

    Ordered so two serializations of one payload are byte-identical — a
    reader diffing two stored games is a reader this makes possible.
    """
    return {
        "engine_version": engine_version_to_primitive(replay.engine_version),
        "variant": replay.variant.value,
        "opening_position": position_to_primitive(replay.opening_position),
        "records": [move_record_to_primitive(record) for record in replay.records],
        "expected_result": (
            None
            if replay.expected_result is None
            else match_result_to_primitive(replay.expected_result)
        ),
    }


def replay_from_primitive(entry: Primitive) -> ReplayData:
    expected = entry["expected_result"]
    return ReplayData(
        engine_version=engine_version_from_primitive(entry["engine_version"]),
        variant=BoardVariant(entry["variant"]),
        opening_position=position_from_primitive(entry["opening_position"]),
        records=tuple(move_record_from_primitive(record) for record in entry["records"]),
        expected_result=None if expected is None else match_result_from_primitive(expected),
    )


def move_records_from_primitive(entries: Sequence[Primitive]) -> tuple[MoveRecord, ...]:
    return tuple(move_record_from_primitive(entry) for entry in entries)


__all__ = [
    "match_result_from_primitive",
    "match_result_to_primitive",
    "move_record_from_primitive",
    "move_record_to_primitive",
    "move_records_from_primitive",
    "replay_from_primitive",
    "replay_to_primitive",
]
