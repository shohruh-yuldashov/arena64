"""The **only** package other modules may import from `users` — BE-03.

Everything else under `app.modules.users` is private. The rule exists
because Python's import system will happily let `game` reach into
`users.infrastructure.models` and query the table directly, and rule R-1
(architecture.md §7) forbids exactly that — but forbidding it in prose does
not stop it at the hundredth pull request. One named surface makes the rule
expressible as a single import-linter contract ("nothing may import
`app.modules.users` except `app.modules.users.public`"), which turns a
convention into a build failure.

What is published, and why only this much:

  `UserId`               the identifier every other context refers to a
                         player by (DM-06: `player_id` is the only
                         cross-context reference)
  `UserRead`             the account holder's own view — what registration
                         returns to the person who just registered
  `UserSummary`          the minimal public view, for rendering who
                         someone is without loading their whole profile
  `UserAccountCreator`   the narrow port `auth` uses to register a user
  `NewUserAccount`       that port's input shape
  `UserCredentialStore`  the equally narrow port `auth` uses to sign one in
  `UserProfileReader`    reads one account's own view by id (A64-011.5)
  `EmailVerifier`        marks an address verified (A64-011.6)
  `EmailRecipientDirectory`
                         who may be **emailed**, and at what address
                         (A64-021.5). Read-only, batch, and eligibility is
                         the absence of a result rather than a flag — see
                         `email_recipients.py`
  `PasswordResetter`     replaces a password hash, and can do nothing
                         else — not even read one (A64-011.7)
  `PublicProfileReader`  reads the view a *stranger* may see, by username
                         (A64-012.1)
  `PublicProfileSearcher` finds players by username or display name
                         (A64-013.1), returning the *same* type — which is
                         what makes search results and profile pages one
                         representation rather than two
  `UserSearchQuery`      that port's input, including the exclusion set
                         blocking will fill
  `SearchTerm`           the value object that decides whether a raw term is
                         searchable at all — published by A64-013.8 because
                         `profiles` was reaching into `users.domain` for it.
                         The rules belong to `users`, which owns handles;
                         the caller that parses is whoever received the
                         request
  `UserSearchPage`       that port's output: ranked identities and a cursor
  `AvatarStore`          reads and writes the avatar *reference* — never
                         image data (A64-012.2)
  `ProfileEditor`        reads and updates the owner's own editable
                         fields — never the username (A64-012.3)
  `ProfileEdits`         that port's input: five optional fields and no
                         others, which is the mass-assignment defence
  `PrivacySettingsEditor` reads and updates who may see what (A64-012.4).
                         Separate from `ProfileEditor` because editing a
                         biography and publishing an account's activity
                         are different authorities
  `PrivacyEdits`         that port's input: five optional booleans
  `PrivacySettingsView`  that port's output — the owner's own five flags
  `PresenceProvider`     reads whether a player is here right now
                         (A64-012.7). Read-only, and separate from the
                         recorder so that the module serving anonymous
                         traffic cannot mark anybody online
  `PresenceOnline`,      the two presence edges this context makes durable
  `PresenceOffline`      through the outbox (A64-013.7). Emitted on
                         transitions only — a token refresh by a player who
                         was already online is not an event
  `PresenceRoster`       who is due to lapse, and forgetting them once they
                         have (A64-013.8). Held only by the presence sweeper,
                         which is what closes the "nobody observes an expiry"
                         gap A64-013.7 left open
  `LapsedPresence`       that port's record — a player and the instant their
                         window closed
  `PresenceRecorder`     writes what was observed of a player. Held only by
                         `PresenceService`, which `auth`'s lifecycle routes
                         resolve (A64-013.6) — and by AD-09's gateway when
                         it exists. Never by a read path
  `Presence`             those ports' record — a frozen dataclass rather
                         than a Pydantic DTO, because `profiles.domain`
                         holds it and a domain layer must not import a
                         framework (architecture.md §8)
  `VisibilityLevel`      who a profile field is published to — `everyone`,
                         `friends`, `nobody` (A64-013.2). Published because
                         `ProfileVisibility` and `PrivacySettingsView` are
                         defined in terms of it (BR-2)
  `ViewerRelationship`   what the caller is to the player being read.
  `RelationshipState`    what the caller may *do* about them — A64-020.4.
                         Computed per request, never stored
  `DeviceType`           the closed set `Presence.device_type` is defined
                         in terms of, published because BR-2 requires a
                         published shape's field types to be published too.
                         Never reaches the wire
  `PreferencesEditor`    reads and updates the owner's personal settings
                         (A64-012.5), and is the *only* way to change a
                         language or a timezone since that task
  `PreferenceEdits`      that port's input, grouped: gameplay and locale
  `PreferencesView`      that port's output. Never public
  `BoardTheme`, `PieceSet`, `AnimationSpeed`
                         the closed sets those shapes are defined in terms
                         of — published because BR-2 requires a published
                         DTO's field types to be published too
  the `DEFAULT_*`        the platform defaults, so a consumer documents the
  preference constants   real ones rather than restating them by hand
  `ProfileVisibility`    the *four* a consumer needs to render somebody
                         else. No `show_country`: that one is applied
                         inside `users`, so it never has to travel
  `OwnUserProfile`       the owner's view — the editable fields plus
                         identity, and no account state
  `AvatarReference`      an object key, a version and a timestamp. No URL:
                         composing one is `StorageProvider`'s
  `PublicUserProfile`    that port's output — deliberately has no `email`
                         field, so the module serving anonymous traffic
                         cannot leak one
  `UserCredentials`      that port's output — an account view plus the
                         stored hash, and nothing that would let a
                         consumer read or edit a profile
  the four exceptions    so a consumer can branch on the outcome

Deliberately **not** published: the `User` entity (it is mutable, and a
consumer holding one could change fields this module is responsible for),
the repository port (R-1: reach a module through its services, never its
storage), and `UserService` itself — `auth` gets the one method it needs
through `UserAccountCreator`, not the whole class.
"""

from uuid import UUID

from app.modules.users.application.services.user_profile_service import (
    UserProfileService,
)
from app.modules.users.domain.events import (
    EmailVerified,
    PresenceOffline,
    PresenceOnline,
    UserRegistered,
)
from app.modules.users.domain.exceptions import (
    EmailAlreadyExists,
    InvalidBio,
    InvalidCountryCode,
    InvalidDisplayName,
    InvalidEmail,
    InvalidUsername,
    UsernameAlreadyExists,
    UserNotFound,
)
from app.modules.users.domain.preferences import (
    DEFAULT_ANIMATION_SPEED,
    DEFAULT_BOARD_THEME,
    DEFAULT_CONFIRM_MOVE,
    DEFAULT_PIECE_SET,
    DEFAULT_SHOW_COORDINATES,
    AnimationSpeed,
    BoardTheme,
    PieceSet,
)
from app.modules.users.domain.presence import DeviceType, LapsedPresence, Presence
from app.modules.users.domain.search import SearchTerm
from app.modules.users.domain.visibility import (
    RelationshipState,
    ViewerRelationship,
    VisibilityLevel,
)
from app.modules.users.public.administration import (
    AdministrativeUserDirectory,
    AdminUserFilters,
    AdminUserPage,
    AdminUserRecord,
)
from app.modules.users.public.audience import NotificationAudienceDirectory
from app.modules.users.public.credentials import UserCredentials
from app.modules.users.public.dtos import (
    AvatarReference,
    GameplayPreferencesView,
    LocalePreferencesView,
    OwnUserProfile,
    PreferencesView,
    PrivacySettingsView,
    ProfileVisibility,
    PublicUserProfile,
    UserRead,
    UserSummary,
)
from app.modules.users.public.edits import (
    GameplayEdits,
    LocaleEdits,
    PreferenceEdits,
    PrivacyEdits,
    ProfileEdits,
)
from app.modules.users.public.email_recipients import (
    EmailRecipient,
    EmailRecipientDirectory,
)
from app.modules.users.public.ports import (
    AvatarStore,
    EmailVerifier,
    NewUserAccount,
    PasswordResetter,
    PreferencesEditor,
    PresenceProvider,
    PresenceRecorder,
    PresenceRoster,
    PrivacySettingsEditor,
    ProfileEditor,
    PublicProfileReader,
    PublicProfileSearcher,
    UserAccountCreator,
    UserCredentialStore,
    UserProfileReader,
)
from app.modules.users.public.search import UserSearchPage, UserSearchQuery

# The cross-context player identifier. An alias rather than a `NewType`
# because it crosses a JSON boundary in both directions and every consumer
# already holds it as a plain UUID; a stricter wrapper would be stripped at
# the first `model_dump()` anyway.
type UserId = UUID

__all__ = [
    "DEFAULT_ANIMATION_SPEED",
    "DEFAULT_BOARD_THEME",
    "DEFAULT_CONFIRM_MOVE",
    "DEFAULT_PIECE_SET",
    "DEFAULT_SHOW_COORDINATES",
    "AnimationSpeed",
    "AvatarReference",
    "AvatarStore",
    "DeviceType",
    "RelationshipState",
    "ViewerRelationship",
    "VisibilityLevel",
    "OwnUserProfile",
    "ProfileEditor",
    "ProfileEdits",
    "EmailAlreadyExists",
    "EmailRecipient",
    "EmailRecipientDirectory",
    "EmailVerifier",
    "InvalidBio",
    "InvalidCountryCode",
    "InvalidDisplayName",
    "InvalidEmail",
    "InvalidUsername",
    "NewUserAccount",
    "BoardTheme",
    "GameplayEdits",
    "GameplayPreferencesView",
    "LocaleEdits",
    "LocalePreferencesView",
    "PasswordResetter",
    "PieceSet",
    "PreferenceEdits",
    "PreferencesEditor",
    "PreferencesView",
    "Presence",
    "PresenceOffline",
    "PresenceOnline",
    "UserRegistered",
    "EmailVerified",
    "LapsedPresence",
    "PresenceProvider",
    "PresenceRecorder",
    "NotificationAudienceDirectory",
    "PresenceRoster",
    "PrivacyEdits",
    "PrivacySettingsEditor",
    "PrivacySettingsView",
    "ProfileVisibility",
    "PublicProfileReader",
    "SearchTerm",
    "PublicProfileSearcher",
    "PublicUserProfile",
    "AdminUserFilters",
    "AdminUserPage",
    "AdminUserRecord",
    "AdministrativeUserDirectory",
    "UserSearchPage",
    "UserSearchQuery",
    "UserAccountCreator",
    "UserCredentialStore",
    "UserCredentials",
    "UserId",
    "UserProfileReader",
    "UserProfileService",
    "UserNotFound",
    "UserRead",
    "UserSummary",
    "UsernameAlreadyExists",
]
