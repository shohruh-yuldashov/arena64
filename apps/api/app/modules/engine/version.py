"""`EngineVersion` — which implementation of the rules a game was played
under.

Framework-free and dependency-free (AD-13).

## Why this exists at all

AD-15: "rules implementations get fixed. If a repetition-detection bug is
corrected in 2027, replaying a 2025 game under the new engine could yield a
different outcome than the one that was rated and displayed. Recording the
version means historical games replay under the semantics they were
actually played under, and it makes the blast radius of a rules fix
precisely measurable — we can enumerate exactly which matches were played
under the defective version."

MT-3 makes it immutable on a match after creation, for the same reason.

## Why it is a constant and not derived at runtime

Not from package metadata, not from a git commit, not from a build
timestamp. Three reasons, in order:

1. **The same source must always stamp the same version.** A version read
   from the environment differs between a container, a developer's laptop
   and a test run, so two identical games would record two different rule
   sets and the enumeration AD-15 promises would be a guess.
2. **It changes when the *rules* change, not when the code does.** A
   refactor, a docstring, a faster loop — none of those alter what is
   legal, and none should invalidate a replay. Bumping this is a
   deliberate act by whoever changed a rule.
3. **It has to be stampable from a pure kernel.** Reading package metadata
   is I/O, which AD-13 forbids and `.importlinter` fails on.

## When to bump it

When a change alters what `MoveGenerator`, `MoveValidator` or
`TerminalStateEvaluator` answer for any position — a rule fixed, a rule
added, a variant's configuration corrected. Then, and only then. The
corpus is the practical test: if a corpus case's expectation changes, the
version changes with it.
"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True, order=True)
class EngineVersion:
    """An ordered, primitive-comparable identifier of a rules build.

    A single integer rather than a semantic-version triple. There is no
    "minor rules change" — either two builds agree about every position or
    they do not — so the extra components would carry no information and
    invite the argument about which one to bump.

    **Ordered**, so "played under a version older than the fix" is a
    comparison rather than a lookup table. That is the query AD-15 exists
    to make answerable.
    """

    number: int

    def __post_init__(self) -> None:
        if self.number < 1:
            raise ValueError("An engine version is a positive integer.")

    def as_primitive(self) -> int:
        """The value to persist and to put on the wire.

        An `int`, so a stored match needs no parsing to compare against a
        build, and a corpus or a replay in another language reproduces it
        exactly. `EngineVersion(n).as_primitive() == n` is the whole
        contract.
        """
        return self.number

    def __str__(self) -> str:
        return f"engine-v{self.number}"


CURRENT_ENGINE_VERSION = EngineVersion(number=1)
"""The rules this build implements — A64-014.1 through A64-014.6.

Version 1 covers: the board, men's and kings' moves, complete capture
sequences, mandatory and maximum capture, all three mid-sequence promotion
rules, and terminal detection by material and by mobility. It does **not**
cover draw rules, which arrive in A64-014.7 and will make it 2 — a game
played under version 1 has no draw-by-repetition and replaying it under
version 2 could end it earlier, which is exactly the divergence AD-15 says
must be recorded rather than discovered.
"""


__all__ = ["CURRENT_ENGINE_VERSION", "EngineVersion"]
