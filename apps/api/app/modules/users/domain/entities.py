"""The `User` domain entity — the thing this module is about.

Framework-free by rule (architecture.md §8: a `domain/` layer imports no
FastAPI, no SQLAlchemy, no Redis, no clock). What it *does* import is
`app.core.enums`, `app.core.identifiers` and `app.core.exceptions`, all of
which are pure standard-library Python with no framework underneath. Those
three are morally the `shared/` kernel that services.md §1 specifies and
A64-006 recorded as not-yet-carved-out; when `shared/` is created they move
there and this import list does not otherwise change.

**Why this exists separately from `infrastructure/models.py::UserModel`.**
repositories.md §4 is explicit that a repository returns domain entities
and never ORM rows, because a returned ORM row carries lazy-loading
behaviour and a session lifetime into layers that have no business knowing
about either — the classic "works in the test that kept the session open,
`MissingGreenlet` in production" failure. The cost is one mapping function
per direction, confined to the repository (repositories.md §3 names that
mapping as the repository's core job). The benefit is that `UserService`
and everything above it cannot touch a database even by accident.
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from app.core.enums import Locale
from app.core.identifiers import generate_uuid7
from app.modules.users.domain.value_objects import Bio, CountryCode, Email, Timezone, Username


@dataclass(slots=True)
class User:
    """A player's identity record.

    Mutable, unlike the value objects it holds: an entity is defined by its
    identity persisting across changes (DM-01), and a profile update is
    exactly such a change. The `id` never changes; everything else may.

    Only fundamental identity lives here. Rating, win/loss counts, presence
    and leaderboard position are each another module's aggregate keyed by
    this `id` (DM-06's rule that `player_id` is the only cross-context
    reference) — adding any of them as a column here is what would make
    this module impossible to separate later.
    """

    id: UUID
    username: Username
    email: Email
    password_hash: str
    preferred_language: Locale
    timezone: Timezone
    is_active: bool
    is_verified: bool
    created_at: datetime
    updated_at: datetime | None = None
    display_name: str | None = None
    avatar_url: str | None = None

    # Presentational identity — domain-model.md §7: `UserProfile` owns
    # "display name, avatar reference, country, biography, join date".
    # Added by A64-012.1, which needs them for the public profile view.
    #
    # Both are `None` for every account today: no endpoint writes them yet
    # (A64-012.1's brief excludes editing). They are real columns rather
    # than hardcoded nulls in the response because the response contract
    # requires the fields, and a column is where the value will come from
    # when the edit endpoint arrives — the alternative is a view that lies
    # about having a data model behind it.
    bio: Bio | None = None
    country: CountryCode | None = None

    # Sign-in is refused until this instant passes. `None` means unlocked.
    #
    # Distinct from `is_active`, and the two are not interchangeable:
    # deactivation is an indefinite state a player or an administrator
    # chooses, whereas a lock is temporary and expires on its own without
    # anything having to run to clear it (domain-model.md's `auth.account`
    # calls this "throttling, not sanction"). Evaluated on read for the
    # same reason `Sanction` expiry is — a job that clears locks and fails
    # leaves people locked out, so the safe direction is for the lock to
    # lapse by itself.
    locked_until: datetime | None = None

    @classmethod
    def create(
        cls,
        *,
        username: Username,
        email: Email,
        password_hash: str,
        preferred_language: Locale,
        timezone: Timezone,
        created_at: datetime,
        display_name: str | None = None,
        avatar_url: str | None = None,
    ) -> "User":
        """Builds a new, never-persisted user.

        `created_at` is a **parameter, not a `datetime.now()` call**, and
        that is the whole reason this factory exists rather than a bare
        constructor: AD-07 forbids the domain from reading the clock, so
        the application layer — which has the injected clock port — passes
        the instant in. It is what makes "a user created at midnight on a
        leap day" a test that runs in microseconds rather than one that
        waits.

        `id` is generated here rather than by the database (DB-07): a
        UUIDv7 minted in Python is known to the caller before the insert,
        which is what a future outbox write referencing this user in the
        same transaction needs (AD-16).

        A new user is `is_active=True` (they can be looked up and updated
        immediately) but `is_verified=False` — nobody has proven they own
        the email yet, and proving it is `auth`'s job, not this module's.
        """
        return cls(
            id=generate_uuid7(),
            username=username,
            email=email,
            password_hash=password_hash,
            preferred_language=preferred_language,
            timezone=timezone,
            is_active=True,
            is_verified=False,
            created_at=created_at,
            updated_at=None,
            display_name=display_name,
            avatar_url=avatar_url,
        )

    @property
    def effective_display_name(self) -> str:
        """What a UI should render. Falls back to the username so that no
        caller has to write `user.display_name or user.username.value` —
        which is the kind of trivial rule that gets written five different
        ways in five templates if the domain does not state it once.
        """
        return self.display_name or self.username.value

    def activate(self) -> None:
        self.is_active = True

    def deactivate(self) -> None:
        """Marks the account unusable without deleting it.

        Deliberately *not* a `deleted_at` (database.md DB-20: no relation
        on this platform uses a generic soft-delete flag). "Deactivated" is
        a reversible state a player can choose; erasure is a separate,
        irreversible obligation with a legal clock (DM-13) that will not
        reuse this method or this column.
        """
        self.is_active = False

    def is_locked_at(self, instant: datetime) -> bool:
        """Whether sign-in is refused at `instant`.

        Takes the instant as a parameter rather than reading the clock —
        AD-07 forbids the domain from doing that, and it is what makes
        "the lock expires one second from now" a test that runs
        instantly.
        """
        return self.locked_until is not None and instant < self.locked_until

    def mark_verified(self) -> None:
        """Records that ownership of the email has been proven.

        Present here — rather than in `auth` — because `is_verified` is a
        column on this entity and only its owner may mutate it. `auth` will
        *decide* verification happened and call this through the published
        port; the invariant that nothing else flips the flag stays here.
        """
        self.is_verified = True
