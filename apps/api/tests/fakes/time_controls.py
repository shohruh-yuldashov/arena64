"""The time control catalogue, in memory — A64-020.5A-pre §23.

Two things live here, and the second one exists because of §16.

## `FakeTimeControlCatalogue`

`reference.public.TimeControlCatalogue` over a dict. It models the one
behaviour every consumer branches on — an identifier that is not offered is
a refusal, not an empty answer — and nothing else, because the port has
nothing else.

## The constants, and why they are only in `tests/`

A64-020.5A-pre §16 forbids a production-facing default time control: every
control is a genuinely different game, so a caller that omitted one would
silently enter a pool they did not pick. `QueuePool.time_control_id` is
therefore required, and dozens of existing tests construct pools that do not
care which control they are for.

§16's own answer is "a narrow temporary constructor default **only in test
fixtures**". `BLITZ` is that default: a test that has no opinion says
`time_control_id=BLITZ.id` and reads as having none, while a test that *is*
about time controls names two and contrasts them.

The values match the seeded catalogue exactly, so a fixture and a real row
describe the same game.
"""

from collections.abc import Sequence

from app.modules.rating.public import SpeedClass
from app.modules.reference.public import (
    TimeControl,
    TimeControlId,
    TimeControlSnapshot,
    UnsupportedTimeControl,
)

#: The four seeded controls, as the migration writes them.
BULLET = TimeControlSnapshot(
    id=TimeControlId.BULLET_1_0,
    base_time_ms=60_000,
    increment_ms=0,
    speed_class=SpeedClass.BULLET,
)
BLITZ = TimeControlSnapshot(
    id=TimeControlId.BLITZ_3_2,
    base_time_ms=180_000,
    increment_ms=2_000,
    speed_class=SpeedClass.BLITZ,
)
RAPID = TimeControlSnapshot(
    id=TimeControlId.RAPID_10_0,
    base_time_ms=600_000,
    increment_ms=0,
    speed_class=SpeedClass.RAPID,
)
CLASSICAL = TimeControlSnapshot(
    id=TimeControlId.CLASSICAL_30_0,
    base_time_ms=1_800_000,
    increment_ms=0,
    speed_class=SpeedClass.CLASSICAL,
)

#: The catalogue as `a3f91c7d5e42` seeds it, in display order.
#:
#: One definition for both suites: the unit tests build a
#: `FakeTimeControlCatalogue` from it, and `tests/contract/conftest.py`
#: writes these rows into a `create_all`-built schema — which has no
#: migration and therefore no seed. Three copies of four rows was the
#: alternative, and the third would have drifted.
SEEDED_TIME_CONTROLS: tuple[tuple[TimeControlSnapshot, str], ...] = (
    (BULLET, "1+0"),
    (BLITZ, "3+2"),
    (RAPID, "10+0"),
    (CLASSICAL, "30+0"),
)


def _entry(snapshot: TimeControlSnapshot, label: str, *, order: int, active: bool) -> TimeControl:
    return TimeControl(
        snapshot=snapshot,
        label=label,
        display_order=order,
        is_active=active,
    )


class FakeTimeControlCatalogue:
    """`TimeControlCatalogue` over a dict, seeded like the migration.

    `retired` takes it out of `active()` and makes `require` refuse it,
    which is the one state transition the catalogue has and the only way to
    reach `UnsupportedTimeControl` for an identifier that is a real enum
    member.
    """

    def __init__(self, *, retired: frozenset[TimeControlId] = frozenset()) -> None:
        self._entries = {
            snapshot.id: _entry(snapshot, label, order=order, active=snapshot.id not in retired)
            for order, (snapshot, label) in enumerate(SEEDED_TIME_CONTROLS)
        }

    async def active(self) -> Sequence[TimeControl]:
        return tuple(
            entry
            for entry in sorted(
                self._entries.values(), key=lambda entry: (entry.display_order, entry.id)
            )
            if entry.is_active
        )

    async def require(self, time_control_id: TimeControlId) -> TimeControl:
        entry = self._entries.get(time_control_id)
        if entry is None or not entry.is_active:
            raise UnsupportedTimeControl("That time control is not available.")
        return entry


__all__ = [
    "BLITZ",
    "BULLET",
    "CLASSICAL",
    "RAPID",
    "SEEDED_TIME_CONTROLS",
    "FakeTimeControlCatalogue",
]
