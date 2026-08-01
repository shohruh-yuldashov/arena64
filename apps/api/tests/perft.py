"""Perft — counting move sequences to a fixed depth.

architecture.md AD-13 names this first among the things that make a rules
kernel trustworthy: "move-generation node counts verified against known
reference values at increasing depth". It is the only check here that can
fail against an **external** oracle rather than against the engine's own
opinion of itself, which is what makes it worth more than every
self-consistency test in the suite put together.

## What it counts

Leaf nodes, in the ordinary sense: `perft(position, 0)` is 1, and
`perft(position, d)` is the sum of `perft(after, d - 1)` over every legal
move. A position with no legal moves contributes nothing at greater depth
— the game ended there, and a finished game has no continuations to count.

## Deliberately naive

No caching, no transposition table, no pruning, no bulk counting at the
last ply. Every one of those is an optimisation that can be *wrong*, and a
perft that shared a bug with the generator it is checking would agree with
it beautifully. This walks the tree the slow way through
`MoveGenerator.legal_moves` and `MoveApplier.apply` — the production
services, re-validating every move exactly as a live game would.

That costs roughly double: the applier validates by generating. It is kept
anyway, because a perft that bypassed validation would stop covering the
half of the engine a player's move actually passes through.

## Why it lives in `tests/`

It is a verification tool, not a rule. Nothing in production counts move
trees, and an unused export in the kernel is an export somebody will
eventually find a use for.
"""

from collections.abc import Mapping

from app.modules.engine import Move, MoveApplier, MoveGenerator, Position


def perft(
    position: Position,
    depth: int,
    generator: MoveGenerator,
    applier: MoveApplier,
) -> int:
    """How many distinct move sequences of exactly `depth` plies exist.

    `depth` 0 counts the position itself, which is the convention every
    published table uses — without it the recursion has no base case and
    the numbers are off by a ply.
    """
    if depth < 0:
        raise ValueError("Perft depth is not negative.")
    if depth == 0:
        return 1

    total = 0
    for move in generator.legal_moves(position):
        total += perft(applier.apply(position, move), depth - 1, generator, applier)
    return total


def perft_divide(
    position: Position,
    depth: int,
    generator: MoveGenerator,
    applier: MoveApplier,
) -> Mapping[Move, int]:
    """`perft` split by first move — the tool for finding *where* a count
    went wrong.

    A total that disagrees with a reference says the generator is wrong
    somewhere; a divide says which opening move's subtree carries the
    difference, and repeating it narrows to the ply. It is unused by the
    assertions in this suite and exists for whoever has to investigate a
    failure.
    """
    return {
        move: perft(applier.apply(position, move), depth - 1, generator, applier)
        for move in generator.legal_moves(position)
    }
