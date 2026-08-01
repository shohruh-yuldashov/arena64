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

from app.core.exceptions import DomainError


class GameDomainError(DomainError):
    """Root of the rules kernel's failures. Never raised directly."""


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


class DestinationOccupied(GameDomainError):
    """A command would have put a piece on a square that already holds one.

    Includes relocating a piece onto itself. Not a capture: capture removes
    the taken piece first and is `game`'s to orchestrate through move
    application, which does not exist yet.
    """


__all__ = [
    "DestinationOccupied",
    "GameDomainError",
    "InvalidBoardState",
    "InvalidCoordinate",
    "PieceNotFound",
]
