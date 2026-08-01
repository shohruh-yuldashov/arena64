"""The rules kernel's typed failures, built on the platform hierarchy in
`app.core.exceptions` — never a parallel one.

Inheriting from the existing tree is what makes these work end to end with
no per-module wiring: `app/api/exception_handlers.py` maps by walking an
exception's MRO, so anything below `DomainError` already renders through
the platform's envelope if it ever reaches a response.

## Why one root, and why it sits under `DomainError`

`GameDomainError` is the single root so that `game` — the only module
allowed to mutate through the engine (architecture.md R-2) — can catch the
kernel's refusals as one category at its own boundary, rather than
enumerating four types that will become a dozen once move generation
lands.

## The one deliberate exception — `IllegalMove` — A64-014.3

`IllegalMove` sits under `app.core.exceptions.RuleViolationError` instead,
and is the only failure here that does **not** descend from
`GameDomainError`. That is the point rather than an oversight: everything
under `GameDomainError` means *the kernel was used wrongly* — a square that
is not a square, a position that could not have arisen, a move whose shape
is malformed — and every one of them is a caller bug that should never
occur in play. `IllegalMove` means *a player was told no*, which is
ordinary, expected traffic on every game ever played.

`game` therefore wants two handlers, not one, and they behave nothing
alike: an illegal move is a message back to one client, a `GameDomainError`
is an incident. Collapsing them under one root would make the common case
and the never-case indistinguishable at the only boundary that has to tell
them apart. `RuleViolationError` already exists for exactly this and its
docstring already names the case — "e.g. an illegal move, once `game`
exists to raise one".

Placing that root under `DomainError` rather than `ValidationError` is a
deliberate call, and `InvalidCoordinate` is the case that makes it
arguable: an off-board square really is malformed input. It sits here
anyway because **the kernel is never called with unvalidated user input**.
AD-13 makes the engine a pure function of values a caller has already
constructed; a coordinate that reaches it came from `game` translating a
wire message, and `game` owns the `422`. What arrives here is a rule
saying no, which is what `DomainError` means (services.md §7.1, BE-07:
"never logged above INFO, never paged on").

## No new wire codes

Per the rule in `app.core.error_codes.ErrorCode`, a class exists for every
distinct failure because server code branches on the type, but a new
*code* is added only where a client must behave differently and the status
plus the endpoint cannot tell it apart. None of these reaches a client
today — the engine has no HTTP surface and `game` does not exist — so all
four ride the inherited `domain_error` code. The task that gives `game` an
endpoint is the one that can judge which of them a client must distinguish.
"""

from app.core.exceptions import DomainError, RuleViolationError


class GameDomainError(DomainError):
    """Root of the rules kernel's failures — every one of them a caller
    bug. Never raised directly, and deliberately not the root of
    `IllegalMove`; see the module docstring."""


class InvalidCoordinate(GameDomainError):
    """A square that cannot be addressed, or cannot hold a piece.

    Two causes, deliberately one type. A coordinate is refused at
    construction when it is outside the largest board the platform
    supports, and refused by a board when it is outside *that* board or is
    one of the light squares draughts never uses (domain-model.md §2.1:
    "one of the 32 playable dark squares on an 8x8 board").

    They are one type because a caller's recourse is identical — the square
    it named is not a square it may name — and because splitting them would
    put board geometry in the vocabulary of an error raised by a value
    object that has no board.
    """


class InvalidBoardState(GameDomainError):
    """A whole position, or a board's geometry, is internally inconsistent.

    Distinct from `InvalidCoordinate` by *what is at fault*: a command
    given a bad square blames the argument, a position that could not have
    arisen from any sequence of legal setup blames the state. This is what
    a future repository rehydrating a stored position hits when the row is
    corrupt, and it is what a misconfigured `BoardGeometry` raises before
    it can produce a board nobody could play on.
    """


class PieceNotFound(GameDomainError):
    """A command named a square that holds no piece.

    Raised by removal and relocation, never by lookup: a square that is
    legitimately empty is a normal outcome modelled in the return type
    (CLAUDE.md §9.8), so `Board.piece_at` answers `None` and does not raise.
    """


class InvalidMove(GameDomainError):
    """A `Move` that is not well-formed — A64-014.2.

    Malformed **shape**, not illegality: a path shorter than two squares, a
    step that goes nowhere, the same piece captured twice. Whether a
    well-formed move is *legal* in a position is `MoveGenerator`'s answer
    and, from A64-014.3, a validator's.

    The distinction matters because the two have different audiences. An
    illegal move is a player being told no, and it is expected traffic; one
    of these is a caller that built a move wrong, and it should never
    happen in play.
    """


class DestinationOccupied(GameDomainError):
    """A command would have put a piece on a square that already holds one.

    Includes relocating a piece onto itself. Not a capture: capture removes
    the taken piece first and is `game`'s to orchestrate through move
    application, which does not exist yet.
    """


class IllegalMove(RuleViolationError):
    """A well-formed move that the rules do not allow here — A64-014.3.

    The distinction from `InvalidMove` is the one that matters, and it is
    not pedantry:

    | | `InvalidMove` | `IllegalMove` |
    | --- | --- | --- |
    | What | The move's *shape* is broken | It is well formed, and not available here |
    | Example | A one-square path | A quiet move while a capture waits |
    | Raised by | `Move.__post_init__` | `MoveValidator`, against a position |
    | Means | A caller built a move wrong | A player was told no |
    | In play | Never happens | Happens constantly |

    So a `Move` can be constructed, logged, compared and sent over a wire
    while being completely illegal, and that is correct: illegality is a
    property of a move *in a position*, not of the move.

    **Why the message says nothing specific.** It names no rule — not "a
    capture is available", not "that is not your piece". Two reasons. The
    honest one: this class is raised by set membership against the
    generated moves (see `MoveValidator`), so the validator genuinely does
    not know which rule excluded it, and inventing a reason would mean
    re-deriving the rules in the one place built to avoid that. The useful
    one: `game` can compute a far better message from the legal move set it
    already has — "you must capture" is `any(move.is_capture for move in
    legal)` — and that is where a player-facing explanation belongs.
    """


__all__ = [
    "DestinationOccupied",
    "GameDomainError",
    "IllegalMove",
    "InvalidBoardState",
    "InvalidCoordinate",
    "InvalidMove",
    "PieceNotFound",
]
