"""Wire schemas for the public profile — A64-012.1.

**Not reused from `auth`, deliberately and by instruction.** The brief says
"Create reusable profile DTOs. Do not reuse authentication schemas", and
the reason holds independently of the instruction: `auth`'s schemas
describe credentials and their exchange, and every one of them is shaped by
what a *caller proving identity* may send and receive. A profile is read by
a stranger. Sharing a type between those two audiences is how a field
added for the account holder appears on an anonymous response.

Reusable in the sense that matters: `ProfileResponse` is composed of four
smaller schemas — `RatingResponse`, `RatingsResponse`, `StatisticsResponse`
— each of which is the natural unit for the endpoints that will want them
next. A leaderboard row needs `RatingResponse`; a match card needs the
identity fields; neither should have to redeclare them.

## Field naming

Two fields are renamed from their domain spelling, both because the wire
name is the one a player understands:

    created_at   -> joined_at    A64-012.1's field list
    country_code -> country      likewise

Everything else keeps the platform's spelling. Renaming for its own sake
would mean two vocabularies to hold in mind; renaming where the brief names
a field is following the contract.
"""

from collections.abc import Mapping
from datetime import datetime
from uuid import UUID

from pydantic import Field

from app.core.dto import BaseResponseDTO
from app.core.enums import Locale
from app.modules.profiles.domain.profile import PublicProfile
from app.modules.profiles.domain.ratings import RatingCategory, RatingSnapshot
from app.modules.users.domain.validators import BIO_MAX_LENGTH


class RatingResponse(BaseResponseDTO):
    """One category's rating.

    An object rather than a bare integer, and the reason is
    domain-model.md PR-6: "provisional ratings are visibly marked
    everywhere they appear". A number cannot carry the mark, and adding it
    later would change `ratings.classic` from an integer to an object —
    breaking every client that had read it. See `domain/ratings.py`.
    """

    rating: int = Field(
        description=(
            "The player's current rating in this category. **Read it "
            "together with `is_provisional`** — a provisional rating is a "
            "starting value, not a measurement."
        ),
        examples=[1500],
    )
    is_provisional: bool = Field(
        description=(
            "`true` while the rating is not yet established — a player with "
            "too few games in this category. Clients must mark these "
            "visibly; an unmarked provisional rating misleads both the "
            "opponent and the matchmaker."
        ),
        examples=[True],
    )
    games_played: int = Field(
        description=(
            "Matches that have moved *this* rating. Not the player's total, "
            "which is `statistics.games_played` and counts every match "
            "including unrated ones."
        ),
        examples=[0],
    )


def _rating_response(snapshot: RatingSnapshot) -> RatingResponse:
    return RatingResponse(
        rating=snapshot.rating,
        is_provisional=snapshot.is_provisional,
        games_played=snapshot.games_played,
    )


class RatingsResponse(BaseResponseDTO):
    """Every rating category a public profile reports.

    Named fields rather than an open map, so a client knows at compile time
    which categories exist and a missing one is impossible. Adding a fourth
    (`bullet`, `correspondence` — both real speed classes this endpoint does
    not yet report) is an additive change.
    """

    classic: RatingResponse
    rapid: RatingResponse
    blitz: RatingResponse

    @classmethod
    def of(cls, ratings: Mapping[RatingCategory, RatingSnapshot]) -> "RatingsResponse":
        """Builds from the domain's category map.

        A classmethod rather than construction at the call site so the
        mapping from domain to wire lives in one place — the same reason
        `auth`'s `TokenPair.of` exists.

        Indexing each category rather than iterating means a provider that
        somehow returned a short map raises `KeyError` here instead of
        producing a response missing a key. `PlayerRatings` makes that
        unreachable by construction; this is the second lock.
        """
        return cls(
            classic=_rating_response(ratings[RatingCategory.CLASSIC]),
            rapid=_rating_response(ratings[RatingCategory.RAPID]),
            blitz=_rating_response(ratings[RatingCategory.BLITZ]),
        )


class StatisticsResponse(BaseResponseDTO):
    """A player's aggregate match record, across every category."""

    games_played: int = Field(
        description="Matches finished, rated and unrated, in every category.",
        examples=[0],
    )
    wins: int = Field(description="Matches won.", examples=[0])
    losses: int = Field(description="Matches lost.", examples=[0])
    draws: int = Field(description="Matches drawn.", examples=[0])
    win_rate: float = Field(
        description=(
            "`wins / games_played`, in `[0, 1]`, to four decimal places. "
            "**Draws are in the denominator**, so this is the proportion of "
            "games won rather than a chess score percentage — a player with "
            "40 wins, 40 draws and 20 losses has a `win_rate` of `0.4`. "
            "`0.0` for a player who has finished no matches."
        ),
        examples=[0.0],
    )


class ProfileResponse(BaseResponseDTO):
    """A player as everyone else sees them.

    Carries no `email`, no credential material and no account state
    (`is_verified`, `locked_until`, `is_active`). That is a property of the
    types this is built from rather than of remembering to omit them: the
    published DTO `profiles` receives from `users` has no such fields, so
    there is nothing here to strip.
    """

    id: UUID = Field(
        description=(
            "The player identifier — stable, public, and the value every "
            "other resource refers to this player by. Not an internal row "
            "id: sessions, tokens and credentials have their own "
            "identifiers and none of them is ever published."
        ),
        examples=["019fb9ea-0a0c-7cec-9c5f-402727c31a96"],
    )
    username: str = Field(
        description=(
            "The handle, in the casing the player chose. Lookup is "
            "case-insensitive, so `/profiles/Alice` and `/profiles/alice` "
            "return this same profile."
        ),
        examples=["player_one"],
    )
    display_name: str | None = Field(
        description=(
            "A free-form name the player renders under. `null` when unset — "
            "clients should fall back to `username`."
        ),
        examples=["Player One"],
    )
    avatar_url: str | None = Field(
        description="Absolute URL of the player's avatar, or `null` when unset.",
        examples=[None],
    )
    country: str | None = Field(
        description=(
            "ISO 3166-1 alpha-2, upper-cased — `GB`, `UZ`. `null` when the "
            "player has not set one, which today is every player: no "
            "endpoint writes it yet."
        ),
        examples=[None],
    )
    language: Locale = Field(
        description="The player's preferred interface language.",
        examples=["en"],
    )
    bio: str | None = Field(
        default=None,
        max_length=BIO_MAX_LENGTH,
        description=(
            f"A short self-description — **plain text, at most "
            f"{BIO_MAX_LENGTH} characters**. Markdown is not supported and "
            "must not be rendered: treat this as text, not markup. Already "
            "trimmed and free of control and bidirectional characters. "
            "`null` when unset, which today is every player."
        ),
        examples=[None],
    )
    joined_at: datetime = Field(
        description="When the account was created, UTC.",
        examples=["2026-08-01T12:00:00Z"],
    )
    last_seen: datetime | None = Field(
        default=None,
        description=(
            "When the player was last seen online, UTC. **Always `null` "
            "today** — presence tracking is not yet implemented, and this "
            "field is present so that clients render 'unknown' rather than "
            "gaining an unexpected key when it is. Do not infer activity "
            "from its absence."
        ),
        examples=[None],
    )
    ratings: RatingsResponse = Field(
        description=(
            "Current rating per category. Every category is always present; "
            "a player who has never played reports a provisional starting "
            "value rather than a missing key."
        ),
    )
    statistics: StatisticsResponse = Field(
        description="Aggregate match record across every category.",
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "id": "019fb9ea-0a0c-7cec-9c5f-402727c31a96",
                    "username": "player_one",
                    "display_name": "Player One",
                    "avatar_url": None,
                    "country": None,
                    "language": "en",
                    "bio": None,
                    "joined_at": "2026-08-01T12:00:00Z",
                    "last_seen": None,
                    "ratings": {
                        "classic": {
                            "rating": 1500,
                            "is_provisional": True,
                            "games_played": 0,
                        },
                        "rapid": {"rating": 1500, "is_provisional": True, "games_played": 0},
                        "blitz": {"rating": 1500, "is_provisional": True, "games_played": 0},
                    },
                    "statistics": {
                        "games_played": 0,
                        "wins": 0,
                        "losses": 0,
                        "draws": 0,
                        "win_rate": 0.0,
                    },
                }
            ]
        }
    }

    @classmethod
    def of(cls, profile: PublicProfile) -> "ProfileResponse":
        """Renders the composed domain view.

        Field by field rather than `model_validate(profile)`, for the
        reason `users.application.mappers` gives: an explicit mapping means
        a field added upstream can never appear on an anonymous response by
        accident. On the one endpoint the whole platform serves to
        strangers, that is worth the lines.
        """
        identity = profile.identity
        statistics = profile.statistics

        return cls(
            id=identity.id,
            username=identity.username,
            display_name=identity.display_name,
            avatar_url=identity.avatar_url,
            country=identity.country,
            language=identity.preferred_language,
            bio=identity.bio,
            joined_at=identity.created_at,
            last_seen=profile.last_seen,
            ratings=RatingsResponse.of(profile.ratings.as_map()),
            statistics=StatisticsResponse(
                games_played=statistics.games_played,
                wins=statistics.wins,
                losses=statistics.losses,
                draws=statistics.draws,
                # Computed by the domain, never stored — see
                # `domain/statistics.py` on why a persisted win rate is a
                # number that can disagree with the counts beside it.
                win_rate=statistics.win_rate,
            ),
        )
