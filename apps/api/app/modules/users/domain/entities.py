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
from app.modules.users.domain.privacy import PrivacySettings
from app.modules.users.domain.value_objects import (
    Bio,
    CountryCode,
    DisplayName,
    Email,
    Timezone,
    Username,
)


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
    display_name: DisplayName | None = None

    # --- avatar (A64-012.2) -------------------------------------------------
    # database.md §4.6: "`avatar_object_key` | `text` | Object-storage key,
    # not a URL". A64-010 stored a full `avatar_url`; that column is gone.
    #
    # A URL bakes the CDN hostname, the bucket and the URL scheme into every
    # row, so changing provider or putting a CDN in front becomes a data
    # migration over the whole table. A key is the object's address and
    # nothing else — `StorageProvider.get_public_url` composes the rest at
    # render time.
    avatar_object_key: str | None = None
    """The stored object, or `None` when the player has no avatar."""

    avatar_uploaded_at: datetime | None = None
    """When the current avatar was stored. `None` exactly when
    `avatar_object_key` is: the two are set and cleared together by
    `set_avatar` and `clear_avatar`, which are the only writers."""

    avatar_version: int = 1
    """A cache-buster, **not** a count of avatars.

    Starts at 1 and increments on every successful upload *and* on every
    delete. Rendered into the avatar URL, so a replaced avatar is a
    different URL and any browser or CDN holding the old one still fetches
    the new.

    That is what makes a long cache lifetime safe on an avatar: the object
    key is random and immutable, and this is the second lock for any
    intermediary that keyed on something else.

    Deliberately not a timestamp. `avatar_uploaded_at` already is one, and
    a counter that is monotonic per user stays legible in a URL and cannot
    go backwards if a clock does.
    """

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

    # Which of the above a stranger may see — A64-012.4. One value object
    # rather than five booleans, because domain-model.md §7.1 puts privacy
    # preferences *inside* the profile as a named group and a type is what
    # keeps that grouping true in code. See `domain/privacy.py`.
    #
    # A frozen default is safe as a dataclass field default precisely
    # because it is frozen — every account starts on the platform defaults
    # and shares one instance until it changes something, at which point
    # `updated()` returns a new one.
    #
    # **Nothing here consults these flags.** The entity holds them; the
    # mappers that build the *published* views apply them (UP-4 — enforced
    # server-side on every read path). An entity that redacted itself
    # would also redact the owner's own view of their own profile.
    privacy: PrivacySettings = PrivacySettings()

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
        display_name: DisplayName | None = None,
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
        )

    # --- avatar transitions --------------------------------------------------
    #
    # Both live on the entity rather than in a service because they enforce
    # one invariant between three columns: the key, its timestamp and the
    # version move together or not at all. A service setting them
    # individually is how a row ends up with a key and no timestamp, or a
    # cleared key whose version never changed — the second of which is a
    # deleted avatar that every cache keeps serving.

    def set_avatar(self, object_key: str, *, at: datetime) -> None:
        """Points the account at a newly stored object.

        Does **not** delete whatever was there before — this entity has no
        access to storage, and mixing a persistence-layer decision into a
        domain transition is how the two get out of order. Removing the
        previous object is `AvatarService.upload`'s, which does it only
        after this write has committed. See that method on why that
        ordering is the one that cannot orphan a file.

        `at` is a parameter rather than a clock read: AD-07 forbids the
        domain from reading the clock.
        """
        self.avatar_object_key = object_key
        self.avatar_uploaded_at = at
        self.avatar_version += 1

    def clear_avatar(self) -> None:
        """Removes the reference and busts every cache holding the old one.

        Increments the version *because* it is a removal, which is the
        non-obvious half: a client or CDN that cached the previous URL has
        no other signal that it should stop. Without the increment, a
        deleted avatar keeps rendering for as long as anything holds it.

        Idempotent in effect but not in the version: clearing twice bumps
        twice. That is correct — the version is a cache key, not a count,
        and a spurious bump costs one refetch while a missed one costs
        correctness.
        """
        self.avatar_object_key = None
        self.avatar_uploaded_at = None
        self.avatar_version += 1

    @property
    def has_avatar(self) -> bool:
        return self.avatar_object_key is not None

    @property
    def effective_display_name(self) -> str:
        """What a UI should render. Falls back to the username so that no
        caller has to write `user.display_name or user.username.value` —
        which is the kind of trivial rule that gets written five different
        ways in five templates if the domain does not state it once.
        """
        return self.display_name.value if self.display_name else self.username.value

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
