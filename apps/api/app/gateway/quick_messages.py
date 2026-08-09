"""The quick-message catalog — A64-023.1 §1, §2.

What a player may say to their opponent during a live match, as a **closed
set of semantic identifiers owned by the server**. There is no free-text
chat on this platform (ADR-004), and this module is where that stops being
a product statement and becomes a type: a frame that does not name a member
of `QuickMessage` is refused, and the refusal has nowhere else to go
because there is no other field on the wire that carries content.

## Why an enum and not a table

`specs/quick-messages.md` §3 asks for a catalog that can gain and lose
entries without a protocol change, and a `StrEnum` gives exactly that — the
transport carries "a string the server recognises", and which strings those
are is a deployment's code rather than its schema. A `quick_message` table
would buy the ability to change the catalogue without a release and would
cost a database read on the hot path of every send, a migration for the
first entry, and a row whose `id` is the real identity while the enum
member is a comment. Reference data that changes at the speed of a product
decision belongs in the artefact that ships with the product decision.

`reference.time_control` is the counter-example and the reason this rule is
not "static data never goes in a table": a time control has *operator*
meaning — it is offered, withdrawn and priced independently of a release.
A quick message has none. Removing `oops` is a product change that also
needs three translations, and translations ship with the client.

## Why the English words are not the identity

`GOOD_GAME` is the identity; `"good_game"` is its wire form; "Yaxshi
o'yin", "Хорошая игра" and "Good game" are three renderings the *client*
chooses between (§8). The server never sends prose, so an Uzbek player and
a Russian player reading the same frame each read their own language, and
adding a fourth locale touches no server code.

The member names read as English because the codebase does; the values are
lowercase snake case because every other enum this platform puts on the
wire is (`agreed_draw`, `not_a_participant`, `light`). One convention.

## Why these six, and why not more

The catalogue is deliberately small, and small in a specific direction —
**every entry is positive or neutral by construction**. That is the whole
of the abuse model at this layer: a taunt cannot be sent because no taunt
exists to send, which is a far stronger guarantee than moderating one
after the fact and is the reason a curated emoji set was *not* added
beside this one. A shrugging face after an opponent's blunder is a taunt
with deniability, and the platform has no moderation surface to adjudicate
it (`admin` is unbuilt).

Six covers the moments a draughts game actually has:

    good_luck    before the first move. The reason §1 permits sending at
                 ply 0 at all — "gl" that arrives after the opening is not
                 the same message
    nice_move    during play, and the only one that is about the board
    well_played  at the end, about how the opponent played
    good_game    at the end, about the game. Distinct from `well_played`
                 in board-game usage and kept apart deliberately: folding
                 them would make the courtesy at the end of a loss read as
                 praise the loser may not mean
    thanks       the answer to any of the above, and what makes the
                 exchange terminate rather than repeat
    oops         a player's own blunder or misclick. The one entry about
                 the *sender*, which is what stops "I made a mistake" from
                 having to be expressed as something about the opponent

A gap here is not neutral: a player who cannot say the ordinary thing
looks for somewhere else to say it, and there must be no somewhere else.
That is the argument for six rather than four, and the argument against
sixty is that every entry is three translations and one more thing that
can be sent at the wrong moment.

## What is deliberately not here

**No emoji reactions as a second concept.** A reaction and a quick message
are the same thing on the wire — a closed identifier, from a participant,
about one live match, rendered by the receiver — and two concepts would be
two handlers, two rate limits and two authorization paths that must not
diverge. A client is free to render `nice_move` as a glyph; presentation
is the client's and costs the protocol nothing. If a future task wants an
entry that is *only* a glyph, it adds a member here and changes no
transport.

**No per-entry metadata** — no category, no icon, no severity. Nothing
consumes one, and a field nobody reads is a field that goes stale.
"""

from enum import StrEnum
from typing import Final

from app.modules.game.public import MatchRecordStatus


class QuickMessage(StrEnum):
    """Everything a player may say. See this module's docstring on the six.

    Closed, so an unknown value is refused at the boundary rather than
    handled somewhere downstream — the same property `MessageType` has and
    for the same reason.
    """

    GOOD_LUCK = "good_luck"
    NICE_MOVE = "nice_move"
    WELL_PLAYED = "well_played"
    GOOD_GAME = "good_game"
    THANKS = "thanks"
    OOPS = "oops"


#: The match states a quick message may be sent in — §1(G), §1(H).
#:
#: `ACTIVE` alone, which decides both ends of the lifecycle at once:
#:
#:     before the first move   permitted. A match is `active` from the
#:                             instant both players accepted, which is
#:                             before ply 1 — so `good_luck` arrives when
#:                             it means something
#:     after the result        refused. Post-game abuse is the largest
#:                             source of reports on competitive platforms,
#:                             and a match that has ended has no further
#:                             conversation this platform needs to carry.
#:                             The rule domain-model.md CT-1 stated for
#:                             match chat, kept for the thing that replaced
#:                             it
#:
#: A frozen set rather than a comparison, so a product decision that opened
#: a post-game window is one line here — at the place that already explains
#: why there is not one today.
#:
#: Note that this is **narrower than the room's own lifetime**: a room
#: survives the match that made it, until its members leave or its TTL
#: lapses. Room membership alone would therefore admit a message into a
#: finished game, which is exactly why the handler reads the roster's
#: status and does not stop at `is_attached`.
SENDABLE_STATES: Final = frozenset({MatchRecordStatus.ACTIVE})


def parse_quick_message(raw: object) -> QuickMessage | None:
    """One payload field as a catalogue member, or `None`.

    Takes `object` rather than `str` deliberately: the value comes off a
    decoded JSON payload, so it may be a number, a list, a nested object or
    absent, and a signature that claimed `str` would be a lie the type
    checker could not catch at the call site. Everything that is not a
    member is one answer — there is no partial match, no case folding and
    no prefix — because every relaxation here is a way for something that
    is not in the catalogue to get in.

    This is the whole of the "arbitrary text cannot be injected" guarantee
    (§9). A body of any length, in any encoding, containing anything at
    all, reaches this function and leaves it as `None`.
    """
    if not isinstance(raw, str):
        return None
    try:
        return QuickMessage(raw)
    except ValueError:
        return None


__all__ = ["SENDABLE_STATES", "QuickMessage", "parse_quick_message"]
