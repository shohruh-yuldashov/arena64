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
from app.modules.avatars.public import AvatarLinks
from app.modules.profiles.domain.profile import PublicProfile
from app.modules.profiles.domain.ratings import RatingCategory, RatingSnapshot
from app.modules.statistics.public import PlayerStatistics
from app.modules.users.domain.validators import BIO_MAX_LENGTH
from app.modules.users.public import RelationshipState


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
    """A player's aggregate competitive record, across every category.

    Rendered wherever a record is shown: on `GET /profiles/{username}` for
    a player who has not hidden it, and on `GET /profile/me` always. One
    schema for both, so the two views cannot drift into reporting different
    fields for the same numbers.

    A64-012.6 widened this from four counts to nine fields. Every addition
    is a value the `statistics` projection stores; nothing here is computed
    by this module and nothing is computed by `profiles`.

    `.of()` below is the single mapping. It takes the published
    `PlayerStatistics` and names every field, so a field added to that
    value object never reaches an anonymous response by accident.
    """

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
            "`0.0` for a player who has finished no matches. Derived on read, "
            "never stored — a stored copy can disagree with the counts printed "
            "beside it."
        ),
        examples=[0.0],
    )
    current_rating: int = Field(
        description=(
            "The player's headline rating. **Read it together with the "
            "`ratings` block**, which reports the same player per speed "
            "category and marks a provisional rating as provisional; this "
            "single number carries no such marker yet because nothing "
            "computes it. Both start at 1500 for a player who has never "
            "played."
        ),
        examples=[1500],
    )
    highest_rating: int = Field(
        description=(
            "The highest rating this player has ever held. Never below "
            "`current_rating`. Equal to it for a player who has never played."
        ),
        examples=[1500],
    )
    current_streak: int = Field(
        description=(
            "The active run, **signed**: positive counts consecutive wins, "
            "negative counts consecutive losses, `0` means the last finished "
            "match was a draw or there is no history. One number rather than a "
            "length plus a kind, so the two can never disagree."
        ),
        examples=[0],
    )
    best_win_streak: int = Field(
        description=(
            "The longest run of consecutive wins this player has ever had. "
            "Never negative — there is no losing equivalent."
        ),
        examples=[0],
    )

    @classmethod
    def of(cls, statistics: PlayerStatistics) -> "StatisticsResponse":
        """Renders the published record.

        Field by field rather than `model_validate(statistics)`, for the
        reason `users.application.mappers` gives: this shape is served to
        anonymous callers, and an implicit conversion would publish
        whatever the `statistics` context adds next — a source watermark, a
        rebuild timestamp, an opponent id — without anyone deciding to.
        """
        return cls(
            games_played=statistics.games_played,
            wins=statistics.wins,
            losses=statistics.losses,
            draws=statistics.draws,
            # Computed by the domain, never stored — see
            # `statistics.domain.statistics` on why a persisted win rate is
            # a number that can disagree with the counts beside it.
            win_rate=statistics.win_rate,
            current_rating=statistics.current_rating,
            highest_rating=statistics.highest_rating,
            current_streak=statistics.current_streak,
            best_win_streak=statistics.best_win_streak,
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
        description=(
            "URL of the player's avatar at up to 512px, or `null` when they have not "
            "set one. Carries a `?v=` cache-buster that changes on every upload and "
            "delete, so the URL may be treated as immutable and cached freely."
        ),
        examples=[None],
    )
    thumbnail_url: str | None = Field(
        description=(
            "URL of the 128px rendition, for listings and match cards. `null` when "
            "the player has no avatar. Rendering a placeholder is the client's "
            "decision — only it knows the size and the surrounding design."
        ),
        examples=[None],
    )
    country: str | None = Field(
        description=(
            "ISO 3166-1 alpha-2, upper-cased — `GB`, `UZ`. `null` when the "
            "player has not set one **or has chosen not to show it**. The two "
            "are deliberately indistinguishable: reporting which is which "
            "would answer the question the privacy setting exists to decline."
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
    relationship: RelationshipState | None = Field(
        default=None,
        description=(
            "What **you** may do about this player — A64-020.4. `null` for an "
            "anonymous reader and on your own profile: there is no viewer to "
            "have a relationship with, and nobody is their own friend. "
            "`none` is different and means *signed in, no relationship*, "
            'which is what an "Add friend" control renders from.\n\n'
            "`blocked` means **you blocked them**. Nothing here ever says "
            "that they blocked you — a player who could tell would have "
            "exactly the information a block withholds."
        ),
        examples=["none"],
    )
    is_online: bool | None = Field(
        default=None,
        description=(
            "Whether the player is connected right now.\n\n"
            "`true` while a connection is open. `false` means the platform "
            "saw them disconnect recently enough to still be able to say so. "
            "**`null` means nothing can be said** — and deliberately does not "
            "distinguish between a player who has hidden their presence "
            "(`show_online_status` off), a player nobody has observed, a "
            "presence record that has since expired, and presence being "
            "temporarily unavailable. Reporting which applies would answer "
            "the question the privacy setting exists to decline.\n\n"
            "Render `null` as 'unknown', never as 'offline'. Presence is "
            "best-effort and decays on a timer, so a briefly stale value is "
            "expected rather than a fault."
        ),
        # `true` rather than `null`, so the schema view shows the value a
        # client actually has to render. The response examples below carry
        # both, because `null` stays the honest answer for a hidden, lapsed
        # or never-observed player.
        examples=[True],
    )
    last_seen: datetime | None = Field(
        default=None,
        description=(
            "When the player was last seen online, UTC.\n\n"
            "**`null` unless the player has turned `show_last_seen` on**, "
            "which is the one privacy setting that is off by default — so "
            "this is `null` for most accounts however active they are. It is "
            "also `null` for a player nobody has observed, for a presence "
            "record that has expired, and when presence is unavailable, and "
            "those cases are deliberately indistinguishable from a player "
            "who opted out.\n\n"
            "Never inferred from anything else the platform stores: an "
            "account's last write and a session's last token exchange are "
            "both something other than a person being present. Do not infer "
            "activity from its absence."
        ),
        examples=[None],
    )
    ratings: RatingsResponse = Field(
        description=(
            "Current rating per category. Every category is always present; "
            "a player who has never played reports a provisional starting "
            "value rather than a missing key. **Ratings are always visible** — "
            "they are what pairing is computed from, and privacy settings do "
            "not cover them."
        ),
    )
    statistics: StatisticsResponse | None = Field(
        description=(
            "Aggregate match record across every category, or `null` when the "
            "player has chosen not to show it. **Never zeroes for a hidden "
            "record** — a zeroed record is indistinguishable from a beginner's, "
            "which would mislead the opponent deciding whether to accept a "
            "challenge. Render `null` as 'not shown', not as 'no games'."
        ),
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                # A player who is online and shows it, on the platform
                # defaults — so `is_online` is reported and `last_seen` is
                # `null`, because `show_last_seen` is the one flag that is
                # off out of the box. That pairing is the *common* case and
                # is worth showing as the first example: a client that
                # assumed the two travel together would render this player
                # as never having been seen.
                {
                    "id": "019fb9ea-0a0c-7cec-9c5f-402727c31a96",
                    "username": "player_one",
                    "display_name": "Player One",
                    "avatar_url": None,
                    "thumbnail_url": None,
                    "country": "GB",
                    "language": "en",
                    "bio": None,
                    "joined_at": "2026-08-01T12:00:00Z",
                    "is_online": True,
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
                        "current_rating": 1500,
                        "highest_rating": 1500,
                        "current_streak": 0,
                        "best_win_streak": 0,
                    },
                },
                # A player about whom nothing may be said: presence hidden,
                # country hidden, record hidden. Every one of them is `null`
                # and none of them says why — which is the same shape a
                # brand-new account with nothing filled in produces.
                {
                    "id": "019fb9ea-0a0c-7cec-9c5f-402727c31a96",
                    "username": "player_one",
                    "display_name": "Player One",
                    "avatar_url": None,
                    "thumbnail_url": None,
                    "country": None,
                    "language": "en",
                    "bio": None,
                    "joined_at": "2026-08-01T12:00:00Z",
                    "is_online": None,
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
                    # `null`, never zeroes — and the ratings above stay
                    # visible, because no privacy flag covers them.
                    "statistics": None,
                },
            ]
        }
    }

    @classmethod
    def of(cls, profile: PublicProfile, avatar: AvatarLinks | None) -> "ProfileResponse":
        """Renders the composed domain view.

        Field by field rather than `model_validate(profile)`, for the
        reason `users.application.mappers` gives: an explicit mapping means
        a field added upstream can never appear on an anonymous response by
        accident. On the one endpoint the whole platform serves to
        strangers, that is worth the lines.

        **`avatar` arrives already rendered.** This schema holds no
        `StorageProvider`, knows no object-key layout and cannot compose a
        URL — `avatars.public.AvatarLinkBuilder` does that and the router
        hands the result in. That is A64-012.2's "avatar URL must be
        generated during response mapping ... `ProfileResponse` should not
        know storage implementation details", and it is structural: there
        is nothing in this module's imports that could reach a bucket name.
        """
        identity = profile.identity
        statistics = profile.statistics

        return cls(
            id=identity.id,
            username=identity.username,
            display_name=identity.display_name,
            avatar_url=avatar.avatar_url if avatar else None,
            thumbnail_url=avatar.thumbnail_url if avatar else None,
            country=identity.country,
            language=identity.preferred_language,
            bio=identity.bio,
            joined_at=identity.created_at,
            # **No privacy check here either.** Both presence fields arrive
            # already gated by `ProfileService`, which applies
            # `show_online_status` and `show_last_seen` independently and
            # skips the read entirely when both are off. This schema holds no
            # flag it could get backwards and no provider it could call —
            # exactly as it holds no `StorageProvider`.
            # **No privacy check here either**, and none is needed: this is
            # a fact about the *reader's own* actions, which they already
            # know. `ProfileService` resolves it — or leaves it `None` for
            # an anonymous reader and for the player's own profile.
            relationship=profile.relationship,
            is_online=profile.is_online,
            last_seen=profile.last_seen,
            ratings=RatingsResponse.of(profile.ratings.as_map()),
            # `None` in, `null` out. **No privacy check here** — by the time
            # this runs, a hidden record has already been declined by
            # `ProfileService`, which never fetched it. This schema holds no
            # flag it could get backwards, exactly as it holds no
            # `StorageProvider` it could build a URL with.
            statistics=(StatisticsResponse.of(statistics) if statistics is not None else None),
        )
