"""The catalogue, as everything outside `reference` may read it.

One protocol and three questions, which are exactly the three
A64-020.5A-pre §5 names: list what is offered, resolve one, refuse the rest.

## Why there is no writer here

Nothing on the platform may add, edit or retire a control through code. The
catalogue is seeded by a migration and edited — if ever — by an operator
against the table, which is what makes it *reference* data rather than
configuration a service can drift.

Publishing a writer would also be the first step towards player-authored
controls, and A64-020.5A-pre §2 forbids those by name: a client submits an
identifier and never a duration. That rule is enforced here by there being
no shape in which a duration could be submitted.
"""

from collections.abc import Sequence
from typing import Protocol

from app.modules.reference.domain.time_control import TimeControl, TimeControlId


class TimeControlCatalogue(Protocol):
    """What time controls the platform offers, and what each one is.

    A read port with three methods rather than one, because the callers are
    genuinely different: a picker lists, a queue join resolves one, and the
    difference between "give me nothing" and "refuse" is the difference
    between rendering a menu and rejecting a request.
    """

    async def active(self) -> Sequence[TimeControl]:
        """Every control a player may currently choose, in display order.

        **Deterministic**, and the order is the catalogue's rather than the
        reader's: two clients rendering the same picker must list the same
        controls in the same order, or a player who learned that the third
        entry is 3+2 is wrong on a different device. Ties broken by
        identifier so the order is total (`display_order` is not unique by
        constraint, and a menu is not a place to discover that).

        Retired controls are **absent**, not marked. A caller that wanted to
        render one would be building a menu of things that cannot be chosen.

        Bounded by construction — the catalogue is a handful of rows — so
        there is no `limit` and no cursor. That is the one read on this
        platform where CLAUDE.md §10.5 does not apply, and it applies
        because the bound is the *table*, not the page.
        """
        ...

    async def require(self, time_control_id: TimeControlId) -> TimeControl:
        """The control `time_control_id` names, if it may still be chosen.

        Raises `UnsupportedTimeControl` for an identifier with no row and
        for one whose row is retired — see that exception on why the two
        are one answer.

        **Raises rather than returning `None`**, unlike almost every other
        read port on this platform, and the asymmetry is deliberate: every
        caller of this method is about to build a durable record from the
        result, so there is no branch to take. A `None` would be checked at
        each call site, and the day one of them forgot, a ticket would be
        written with no time control at all.
        """
        ...


__all__ = ["TimeControlCatalogue"]
