"""The ports other modules may depend on — BE-03's published surface.

A64-010 deliberately published none, on the grounds that "publishing a
port before there is a caller is speculative generality... The first real
consumer adds the narrow port it actually needs." `auth` (A64-011.1) is
that first consumer, and `UserAccountCreator` is that narrow port: one
method, exactly what registration needs, and nothing that would let a
consumer read or mutate anything else.

A64-011.2 added the second: `UserCredentialStore`, for login. It is a
separate protocol rather than two more methods on `UserAccountCreator`
because registration and sign-in are different consumers with different
risk — a future component that may create accounts must not automatically
gain the ability to read password hashes.

Note what is *still* not published: there is no way here to fetch an
arbitrary user, list users, or change a profile. The value of a published
surface is entirely in what it withholds.

The two presence ports (A64-012.7) are the clearest case of that principle
so far: `PresenceProvider` can read presence and nothing else,
`PresenceRecorder` can write it and cannot read it back, and the same two
adapters satisfy both. `profiles` is handed only the reader, so the module
serving the platform's highest-volume anonymous read cannot mark anybody
online.
"""

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Protocol
from uuid import UUID

if TYPE_CHECKING:
    from app.modules.users.public.edits import PreferenceEdits, PrivacyEdits, ProfileEdits

from app.modules.users.domain.presence import DeviceType, Presence
from app.modules.users.public.credentials import UserCredentials
from app.modules.users.public.dtos import (
    AvatarReference,
    OwnUserProfile,
    PreferencesView,
    PrivacySettingsView,
    PublicUserProfile,
    UserRead,
)
from app.modules.users.public.search import UserSearchPage, UserSearchQuery


class NewUserAccount(Protocol):
    """The shape `UserAccountCreator.create` accepts.

    A `Protocol` rather than a concrete dataclass so that `auth` can pass
    its own command object without either module importing the other's
    internals — `auth` builds a `RegisterUser`, and it satisfies this
    structurally.

    `password_hash` is an **already-hashed** credential. This module does
    not hash, verify, or inspect it; whoever calls the port has already
    applied the platform's hashing parameters. That split is the whole
    reason `auth` exists: hashing cost, algorithm choice and rotation are
    security decisions with an owner, and it is not `users`.
    """

    @property
    def username(self) -> str: ...

    @property
    def email(self) -> str: ...

    @property
    def password_hash(self) -> str: ...

    @property
    def preferred_language(self) -> str: ...

    @property
    def timezone(self) -> str: ...

    @property
    def display_name(self) -> str | None: ...


class UserAccountCreator(Protocol):
    """Creates a user account and returns its public view.

    Raises `UsernameAlreadyExists` / `EmailAlreadyExists` (also published
    from this package) when a uniqueness rule rejects the request, and the
    relevant `Invalid*` error when a field fails validation. A caller
    branches on those types; it never inspects a message.

    The whole call is one transaction, committed before it returns. A
    caller must not wrap it in a transaction of its own — services.md
    BE-05 forbids a cross-module service call inside an open transaction,
    because the resulting lock-acquisition order is something nobody can
    reason about and a partial failure would leave one module committed
    and the other rolled back with no record that reconciliation is owed.
    """

    async def create(self, account: NewUserAccount) -> UserRead: ...


class UserCredentialStore(Protocol):
    """Reads the credential material for a sign-in, and accepts a rehash.

    Two methods, both of which `auth` demonstrably needs and neither of
    which lets a consumer do anything else — it cannot list accounts,
    cannot read a hash by user id, and cannot set a hash to an arbitrary
    value without already knowing the current one.
    """

    async def find_credentials_by_email(self, email: str) -> UserCredentials | None:
        """`None` when no account has that address — **not** an exception.

        This is the one place in the module where absence must be an
        ordinary return value rather than a `UserNotFound`. `auth` has to
        take exactly the same code path, spending exactly the same time,
        for a known and an unknown address; an exception here would make
        "unknown" the cheap branch and hand an attacker an oracle for
        which addresses have accounts. `UserService.find_by_email` raises
        and stays as it is — this is a different question with a
        different answer.

        Matching is case-insensitive (AC-1): the address is normalised the
        same way registration normalised it before storing.
        """
        ...

    async def replace_password_hash(
        self,
        user_id: UUID,
        *,
        expected_hash: str,
        new_hash: str,
    ) -> bool:
        """Upgrades a stored hash in place, returning whether it applied.

        `expected_hash` makes this a compare-and-swap rather than a blind
        write, and that matters even before a change-password flow exists:
        a rehash-on-login is computed from a hash read *earlier* in the
        request, and an unconditional `UPDATE` would silently revert a
        credential changed in between — turning a security upgrade into a
        credential rollback. `False` means the row moved underneath us,
        which is a no-op, not a failure.

        Never changes what the password *is*: the caller has already
        verified the same plaintext against `expected_hash`, and
        `new_hash` encodes that identical plaintext under stronger
        parameters (database.md §14.2's rehash-on-login).
        """
        ...


class UserProfileReader(Protocol):
    """Reads one account's own view by identifier.

    The third narrow port, added by A64-011.5 for `GET /auth/me` and
    `POST /auth/refresh`. Both need the *profile* behind an identifier the
    caller has already proven — a token's `sub`, or a session's
    `user_id` — and neither may reach into `users`' internals to get it
    (R-1: reach a module through its services, never its storage).

    Separate from `UserAccountCreator` and `UserCredentialStore` for the
    reason those are separate from each other: creating an account,
    reading a password hash and reading a profile are three different
    capabilities, and a component granted one should not thereby hold the
    others. A single `UserService`-shaped port would hand `auth` the
    ability to rename people.

    Read-only by construction. There is no way here to change anything,
    which is what makes it safe to hand to any module that needs to render
    "who is this".
    """

    async def get_profile(self, user_id: UUID) -> UserRead:
        """The account's own view.

        Raises `UserNotFound` rather than returning `None`: every caller
        holds an identifier it has already authenticated, so absence is a
        genuine failure — an account deleted while a valid token was still
        in flight — rather than an ordinary outcome. Making that the
        exceptional path means no caller writes `if user is None` on a
        branch that should never be taken.
        """
        ...

    async def find_by_email(self, email: str) -> UserRead | None:
        """The same view, by address, **returning `None` when absent.**

        The opposite convention to `get_profile` above, and deliberately
        so — the difference is what the caller knows. `get_profile` takes
        an identifier the caller already authenticated, so absence is a
        failure. This takes an address a *stranger* typed into a resend
        form, where absence is the most ordinary outcome there is.

        Raising here would be the leak rather than a nicety:
        `EmailVerificationService.resend_verification` must behave
        identically for a known and an unknown address, and an exception
        is a branch. Matching is case-insensitive (AC-1).
        """
        ...


class EmailVerifier(Protocol):
    """Marks an account's address verified.

    The fourth narrow port, added by A64-011.6. `auth` owns the *proof* —
    it issues the token, checks the expiry and enforces one-time use —
    and `users` owns the *column*. This port is the seam between them,
    and it is one method wide for the reason the other three are: a
    component that may confirm an email must not thereby gain the ability
    to rename people or read password hashes.

    Deliberately **not** paired with an `unverify`. Nothing on the
    platform un-proves ownership of an address; an email *change* is a
    different transition that A64-011.7 or later will model as verifying
    a new address, not as reverting this one.
    """

    async def mark_email_verified(self, user_id: UUID) -> UserRead:
        """Idempotent — verifying an already-verified account succeeds and
        writes nothing. Raises `UserNotFound` if the account is gone,
        which on this flow means it was deleted between the token being
        issued and the link being clicked.
        """
        ...


class PasswordResetter(Protocol):
    """Replaces an account's stored password hash.

    The fifth narrow port, added by A64-011.7. The split is the same one
    the other four make and it lands harder here than anywhere: `auth`
    owns the *proof* — it issues the reset token, checks the expiry,
    enforces one-time use, and computes the Argon2id hash — while `users`
    owns the *column*. Neither can do the other's half.

    **Separate from `UserCredentialStore`, which already has a
    password-writing method.** That is not an oversight and merging them
    would be a real loss. `UserCredentialStore` exists to serve sign-in:
    it can read a password hash, and its write is a compare-and-swap that
    can only re-encode a password the caller has already verified. This
    port cannot read anything and its write is unconditional. A component
    holding both could read a hash and then overwrite it; a component
    holding only this one can replace a credential but never learn what it
    was replacing, which is precisely the authority a reset flow needs and
    the most it should have.

    Deliberately **not** paired with a `get_password_hash`. Nothing about
    a reset requires knowing the old credential — that is what makes it a
    *reset* rather than a change — and a reader here would hand the
    published surface the one capability `UserCredentialStore` keeps
    behind an email lookup.
    """

    async def reset_password(self, user_id: UUID, *, new_hash: str) -> None:
        """Stores `new_hash` as the account's credential, unconditionally.

        No `expected_hash`, unlike `UserCredentialStore`. A recovery flow
        must win a concurrent write rather than lose it: by the time this
        is called the caller has consumed a one-time token and is about to
        revoke every session, and there is nothing useful it could do with
        a declined compare-and-swap except leave somebody locked out of
        their own account.

        Raises `UserNotFound` if the account is gone, which on this flow
        means it was deleted between the link being issued and clicked.
        Returns `None`: absence is raised, so there is no second outcome
        for a return value to report.

        **Does not verify, sanitise or inspect `new_hash`.** It is an
        opaque already-hashed credential, exactly as on `NewUserAccount`.
        The password policy is `auth`'s, applied before hashing.
        """
        ...


class PublicProfileReader(Protocol):
    """Reads the view a stranger may see, by username.

    The sixth narrow port, added by A64-012.1 for `GET /profiles/{username}`.

    **Separate from `UserProfileReader` even though both read profiles**,
    and the split is the whole security design of that endpoint rather
    than bookkeeping. `UserProfileReader` returns `UserRead`, which carries
    the account holder's own email; it exists for `GET /auth/me` and for
    the refresh path, where the caller has already proven the identity it
    is reading. This one is reached by an anonymous request naming somebody
    else, so it returns `PublicUserProfile` — a type with no email field at
    all.

    The consequence worth stating: the `profiles` module cannot leak an
    address, because it is never handed one. That is a stronger guarantee
    than a code review, and it survives a `model_dump()` written by
    somebody who has not read this docstring.

    Read-only by construction, like `UserProfileReader`. There is no way
    here to change anything, which is what makes it safe to hand to a
    module serving unauthenticated traffic.
    """

    async def find_public_profile(self, username: str) -> PublicUserProfile | None:
        """The public view of the account holding `username`, or `None`.

        **Case-insensitive** (UP-1): matching is on the folded form, the
        same one uniqueness is enforced on, so `Alice`, `alice` and `ALICE`
        resolve to one account. The returned `username` preserves the
        casing the player chose.

        Returns `None` rather than raising, and that is a deliberate
        difference from `UserProfileReader.get_profile`. The difference is
        what the caller knows: `get_profile` takes an identifier the caller
        has already authenticated, so absence is a genuine failure. This
        takes a name a stranger typed into a URL, where absence is the most
        ordinary outcome there is — and an exception is a branch, which on
        a public endpoint is a thing that can be timed.

        **A deactivated account has no public profile** and is reported as
        `None`, identically to a username nobody registered — the same
        return, with nothing for a caller to branch on.

        That rule is enforced here rather than by the consumer because
        `users` owns `is_active`. The alternative would be publishing the
        flag on `PublicUserProfile` so a consumer could apply it, and
        "which accounts are deactivated" is itself a disclosure — see
        `PublicProfileService` for the argument. A consumer of this port
        cannot render a withdrawn account even if it tries.
        """
        ...

    async def find_public_profiles(
        self, player_ids: Sequence[UUID]
    ) -> Mapping[UUID, PublicUserProfile]:
        """The same view for a page of players, by id, in **one** query.

        Added by A64-013.2, whose friend-request lists hold ids rather than
        usernames and render a page of them at once. Looking each up through
        `find_public_profile` would be a username the caller does not have,
        and looping any by-id read is the N+1 pattern CLAUDE.md §10.4 names.

        **Deactivated accounts are omitted**, exactly as they are reported
        as `None` above and for the same reason: `users` owns `is_active`,
        and which handles belong to withdrawn accounts is itself a
        disclosure. A caller therefore cannot assume the mapping has an
        entry per id — it must skip what is missing, which is the same
        behaviour it would get from four `None`s.

        Never raises for an unknown id. An empty sequence returns an empty
        mapping without touching the database.
        """
        ...


class AvatarStore(Protocol):
    """Reads and writes a player's avatar *reference* — never the image.

    The seventh narrow port, added by A64-012.2. The split it draws is the
    same one `auth` and `users` already draw over passwords: `avatars` owns
    the *bytes* — validating them, stripping metadata, resizing, encoding,
    storing — and `users` owns the *columns*. Neither can do the other's
    half, and the port is the seam.

    Three methods, all about a reference and none about an image. There is
    deliberately no `upload`, no `delete_image`, and nothing taking `bytes`:
    a port that accepted image data would make `users` the module that has
    to know what a valid image is, which is precisely the coupling this
    exists to prevent.

    **Every write moves three columns together** — key, timestamp and
    version — because they are one fact. `User.set_avatar` and
    `User.clear_avatar` enforce that, and a database CHECK enforces it
    again (BE-06).
    """

    async def get_avatar(self, user_id: UUID) -> AvatarReference:
        """The account's current avatar reference.

        Returns a reference with `object_key=None` for a player who has
        none — never `None` itself, and never raising for that case. The
        version is meaningful either way, so "no avatar" is a *value* here
        rather than an absence, and a caller renders it without a null
        check.

        Raises `UserNotFound` if the account is gone: every caller holds an
        identifier it has already authenticated, so absence is a genuine
        failure rather than an ordinary outcome.
        """
        ...

    async def set_avatar(self, user_id: UUID, *, object_key: str) -> AvatarReference:
        """Points the account at a newly stored object and bumps the
        version. Returns the new reference.

        Returns rather than yielding `None` because the caller needs the
        new version *immediately* — it goes into the URL of the response to
        the very upload that caused it, and a second read to fetch it would
        be a round trip for a number this call just computed.

        Does not touch storage. The previous object is still there when
        this returns, and removing it is the caller's next step — see
        `AvatarService.upload` on why that ordering is the one that cannot
        orphan a file.

        Raises `UserNotFound` if the account is gone.
        """
        ...

    async def clear_avatar(self, user_id: UUID) -> AvatarReference:
        """Removes the reference and bumps the version. Returns the new
        reference, whose `object_key` is `None`.

        Bumping on a *removal* is the non-obvious half and is required by
        A64-012.2 ("deleting must ... increment avatar_version"): a client
        or CDN holding the previous URL has no other signal to stop serving
        it.

        Idempotent for a player who has no avatar — it succeeds and bumps
        the version anyway. A caller retrying after a dropped response must
        not receive an error for the retry, and a spurious cache bust costs
        one refetch while a missed one costs correctness.
        """
        ...


class ProfileEditor(Protocol):
    """Reads and updates the account holder's own editable profile.

    The eighth narrow port, added by A64-012.3, and the first published one
    that *writes* a field a player chose rather than a credential or a
    reference the platform computed.

    **Separate from `PublicProfileReader` even though both read a profile.**
    That one serves anonymous callers and returns a shape with no email and
    no timezone; this one serves the owner and returns `OwnUserProfile`,
    which carries the timezone precisely because it is the owner's to see.
    A single port with a "is this the owner" flag would put the disclosure
    decision on the caller, which is where it must never live.

    **No `username` anywhere on this port**, and that is the architectural
    preparation A64-012.3 asks for rather than an omission. A rename is not
    a profile edit: UP-2 requires the old handle to be recorded and UP-3
    requires a reuse cooldown, so it needs its own use case, its own
    conflict handling and its own rate limit. When it arrives it is a new
    method (`change_username`) or a new port — not a field on `ProfileEdits`
    — so that nothing which merely edits a profile can rename an account.

    Read-only fields cannot be smuggled in either: `ProfileEdits` has five
    attributes and no others, so a caller has nothing to set beyond them.
    """

    async def get_own_profile(self, user_id: UUID) -> OwnUserProfile:
        """The owner's view of their own profile.

        Raises `UserNotFound` rather than returning `None`: the caller
        holds an identifier it has already authenticated, so absence is a
        genuine failure — an account deleted while a valid token was still
        in flight — rather than an ordinary outcome.
        """
        ...

    async def update_own_profile(self, user_id: UUID, edits: "ProfileEdits") -> OwnUserProfile:
        """Applies a partial update and returns the resulting profile.

        **Partial in the PATCH sense**: every field on `ProfileEdits`
        defaults to `UNSET`, and only the ones a caller actually set are
        touched. That distinction is not cosmetic — it is what separates
        "leave the bio alone" from "clear the bio", which a shape using
        `None` for both could not express.

        Returns the updated profile so a client renders the result without
        a second round trip, and so the response reflects **normalised**
        values: a display name arrives padded and comes back trimmed, a
        country arrives `uz` and comes back `UZ`. A caller that echoed its
        own request would show the player something the platform did not
        store.

        Raises `InvalidDisplayName`, `InvalidBio`, `InvalidCountryCode`,
        `InvalidLanguage` or `InvalidTimezone` — all published from this
        package — when a value fails validation, and `UserNotFound` if the
        account is gone. Nothing is written when any field is rejected: the
        values are validated before the entity is touched.
        """
        ...


class PrivacySettingsEditor(Protocol):
    """Reads and updates the account holder's own privacy controls.

    The ninth narrow port, added by A64-012.4.

    **Separate from `ProfileEditor` even though both are self-service edits
    reached from the same settings screen.** The split is the one this
    module makes eight times over, and here it separates two things with
    genuinely different consequences: `ProfileEditor` writes what a player
    says about themselves, and this writes what strangers may learn about
    them. A component granted the first — a future onboarding wizard, a
    moderation tool fixing an abusive display name — must not thereby be
    able to publish an account's activity.

    It is also the port that would be wrong to widen. `PrivacyEdits` has
    five attributes; there is no `is_active`, no way to read another
    account's settings, and no way to set somebody *else's* — the user id a
    caller passes is one it has already authenticated, exactly as on
    `ProfileEditor`.

    ## Why the read is here rather than on `ProfileEditor`

    `OwnUserProfile` deliberately carries no privacy flags. A settings
    screen loads the two independently and a profile edit should not
    restate five booleans it did not touch — but the real reason is the
    same as the split above: a component that may read a biography has no
    claim to know which parts of the account are hidden.
    """

    async def get_privacy_settings(self, user_id: UUID) -> PrivacySettingsView:
        """The owner's own privacy controls.

        Raises `UserNotFound` rather than returning `None`, for the reason
        every other read on this surface does: the caller holds an
        identifier it has already authenticated, so absence means the
        account was deleted while a valid token was in flight.

        Never returns a partial or absent settings object. An account
        always has all five answers — the columns are `NOT NULL` and the
        entity defaults them — so there is no state in which a consumer
        would have to invent one.
        """
        ...

    async def update_privacy_settings(
        self, user_id: UUID, edits: "PrivacyEdits"
    ) -> PrivacySettingsView:
        """Applies a partial update and returns the resulting settings.

        **Partial in the PATCH sense**: every field on `PrivacyEdits`
        defaults to `UNSET`, and only the flags a caller actually set are
        touched. Sending `{"show_country": false}` does not silently reset
        the other four to their defaults — which is the failure mode that
        matters here, because the value it would silently reset
        `show_last_seen` to is the one the player deliberately turned off.

        Returns the full settings rather than an acknowledgement, so a
        client renders every toggle from the response instead of applying
        its own optimistic update and drifting from what was stored.

        Raises `UserNotFound` if the account is gone. Nothing else: there
        is no invalid combination of five independent booleans, so there is
        no validation error this can produce. A malformed *body* is
        rejected at the HTTP boundary before reaching here.
        """
        ...


class PreferencesEditor(Protocol):
    """Reads and updates the account holder's own personal settings.

    The tenth narrow port, added by A64-012.5, and the one that finally
    gives `preferred_language` and `timezone` a single owner. Both were
    writable through `ProfileEditor` until this task; they are not any
    more, so there is exactly one published way to change a language and it
    is the same one that changes a board theme.

    **Separate from `ProfileEditor` and from `PrivacySettingsEditor`**, and
    the three-way split is the same argument each pair already makes.
    A profile edit changes what a player *says about themselves*; a privacy
    flag changes what *strangers may see*; a preference changes what
    *they themselves see*. A component granted the last has no claim to the
    first two, and vice versa — a settings screen that could rename people
    because it may set a board theme would be the widest published surface
    on the platform for the least reason.

    Nothing here is public. `PreferencesView` appears on no anonymous read
    path, and `PublicUserProfile` carries none of its fields — A64-012.5's
    "preferences are never exposed publicly" is a property of the published
    types rather than a filter somebody applies.
    """

    async def get_preferences(self, user_id: UUID) -> PreferencesView:
        """The owner's own settings, every group, always.

        Raises `UserNotFound` rather than returning `None`, for the reason
        every other read on this surface does: the caller holds an
        identifier it has already authenticated, so absence means the
        account was deleted while a valid token was in flight.

        Never partial. Every group is present and every setting inside it
        has a value — an account that has never opened a settings screen
        reports the platform defaults rather than an empty object, so a
        client renders every control from one response and never has to
        know what the defaults are.
        """
        ...

    async def update_preferences(self, user_id: UUID, edits: "PreferenceEdits") -> PreferencesView:
        """Applies a partial update and returns every group.

        **Partial at two levels.** An omitted group is untouched; inside a
        present group, an omitted setting is untouched. Sending
        `{"gameplay": {"board_theme": "wood"}}` does not reset a timezone
        and does not reset the other four gameplay settings.

        Returns the complete settings rather than an acknowledgement, so a
        client renders every control from the response instead of applying
        an optimistic update and drifting from what was stored.

        Raises `InvalidTimezone` — published from this package — when a
        timezone is not an IANA name this system knows, and `UserNotFound`
        if the account is gone. Nothing is written when any value is
        rejected: the timezone is constructed before the entity is touched,
        so a request with a good board theme and a bad timezone changes
        neither.

        The enum-valued settings cannot fail here at all; they arrive
        already narrowed to a member, and a request carrying an unknown
        board theme was rejected at the HTTP boundary with a 422 naming it.
        """
        ...


class PublicProfileSearcher(Protocol):
    """Finds players by username or display name — A64-013.1.

    The thirteenth narrow port, and the second read path by which a caller
    obtains a `PublicUserProfile`. It returns **exactly the type
    `PublicProfileReader` returns**, which is the whole design rather than a
    convenience: A64-013.1 requires that "search results use the same public
    representation as profile pages", and sharing the DTO is the form of
    that requirement which cannot be undone downstream.

    A second, search-shaped identity type would be a second place every
    privacy rule has to be applied — `show_country` is applied by
    `to_public_profile` before either port returns, and a parallel mapper
    would eventually forget it.

    ## What this port refuses to do

    **It does not compose.** No statistics, no presence, no ratings: those
    come from contexts `users` does not own, and this port publishes the
    same `visibility` flags `PublicProfileReader` does so the consumer can
    apply them. `profiles` composes both paths through one code path — see
    `PublicProfileComposer`.

    **It does not rank by anything a caller supplies.** The ordering is
    fixed (exact username, then username prefix, then display-name prefix,
    then partial) and is not a parameter. A client-supplied sort over a
    partial-match query is a way to ask "who exists" from several angles,
    which is the enumeration this endpoint is rate limited against.

    **It never returns a deactivated account**, exactly as
    `PublicProfileReader.find_public_profile` never does, and for the same
    reason: `users` owns `is_active`, and which handles belong to withdrawn
    accounts is itself a disclosure.
    """

    async def search_public_profiles(self, query: UserSearchQuery) -> UserSearchPage:
        """One page of matches, ranked and keyset-paginated.

        Returns an **empty page** for a term nobody matches — never raising,
        and never distinguishable from a term that matched only people the
        caller may not see. A 404 here would answer "does anybody called
        this exist", which is the question an enumeration probe asks.

        Raises `InvalidSearchTerm` when the term fails the rules in
        `users.domain.search`, and `InvalidSearchCursor` when the cursor is
        malformed or was issued for a different term. Both are `422`.

        **Case-insensitive and accent-insensitive**, to the extent the
        database supports it: matching is on a normalised form of both
        sides, and the normalisation is PostgreSQL's own so the term and
        the column can never drift apart.
        """
        ...


class PresenceProvider(Protocol):
    """Reads whether a player is here right now — A64-012.7.

    The eleventh narrow port, and the first that reads something no
    PostgreSQL row holds. domain-model.md §299 assigns `Presence` to this
    module and Redis to the store; `RedisPresenceProvider` and
    `NoPresenceProvider` in `infrastructure/presence/` are the two
    implementations, and the composition root chooses between them.

    **Read-only by construction**, and separate from `PresenceRecorder`
    below for the reason `PublicProfileReader` is separate from
    `ProfileEditor`: `profiles` serves anonymous traffic and must be able to
    *render* presence without being able to *assert* it. A single port with
    both halves would let the public profile endpoint mark accounts online.

    ## Applies no privacy

    `show_online_status` and `show_last_seen` are `users` flags, but they are
    applied by the consumer that composes a public profile — see
    `profiles.application.services.ProfileService`, which declines to call
    this at all when both are off. Two reasons, the second of which is the
    one that lasts: a check here would be a second copy of a rule that
    already has an owner (`PrivacySettings`), and the owner's own view at
    `GET /profile/me` deliberately bypasses privacy entirely, so a port that
    enforced it would need a "but not for the owner" flag — which is exactly
    the disclosure decision that must never sit on a caller.

    ## Designed for more than one node

    A player's presence is one key, written by whichever gateway node holds
    their socket and read by every API node. There is no node affinity, no
    per-node registry to consult and no coordination: the last writer wins,
    and the record expires on its own if every writer stops. That is what
    makes this correct on one process and on fifty without changing.
    """

    async def presence_for(self, player_id: UUID) -> Presence | None:
        """This player's last observed presence, or `None`.

        **`None` is an ordinary outcome, not a failure**, and it deliberately
        collapses three different situations into one answer: the presence
        window has expired, presence has never been recorded for this
        account, or the store could not be reached. A caller cannot tell them
        apart and must not try — `profiles` renders all three as `null`, in
        the same way a hidden field is `null`.

        Distinguishing them would defeat the purpose. "No record because the
        window expired" is a statement about when somebody was last online,
        which is precisely what `show_last_seen` exists to withhold.

        **Never raises.** An unreachable Redis degrades presence to unknown
        rather than failing the profile read behind it — a stale or missing
        online indicator is a cosmetic defect (system-design.md §626), and
        taking down the platform's most-read public endpoint for one would be
        the self-inflicted outage T-2 warns about.

        Takes a `UUID` — DM-06's `player_id`. Deliberately not a username or
        a profile: a presence store has no business receiving a display name.
        """
        ...

    async def presence_for_many(self, player_ids: Sequence[UUID]) -> Mapping[UUID, Presence]:
        """The same answer for a page of players, in **one** round trip.

        Added by A64-013.1, which is the first caller that renders more than
        one player at a time. Calling `presence_for` in a loop over twenty
        search results is twenty round trips to render an indicator, which
        is exactly the N+1 access pattern CLAUDE.md §10.4 names as "the
        single most common cause of slow endpoints" — and it would arrive
        again with friend lists, match cards and leaderboards.

        **Absence is omission, not a `None` value.** The returned mapping
        contains an entry only for players with something to report, so a
        caller writes `presence.get(player_id)` and gets the same `None`
        that `presence_for` returns for an unobserved player. A mapping
        padded with explicit `None`s would carry the same information in a
        shape that invites `presence[player_id]` to raise on the ordinary
        case.

        **Never raises**, and degrades to an empty mapping — the promise
        `presence_for` makes, applied to a page. A search that failed
        because an online indicator could not be computed would be a far
        worse outcome than one that renders without it.

        An empty `player_ids` returns an empty mapping without touching the
        store. That is not a special case for its own sake: an empty page is
        the ordinary result of a search nobody matches, and issuing a
        zero-key `MGET` to learn nothing is a round trip spent on the most
        common failed query.
        """
        ...


class PresenceRecorder(Protocol):
    """Writes what a gateway node observed — A64-012.7.

    The twelfth narrow port, and the write half of presence.
    `RedisPresenceProvider` and `NoPresenceProvider` satisfy this as well as
    `PresenceProvider`; nothing on the HTTP surface holds it.

    ## Why this exists before its caller does

    `statistics.application.ports` declines to publish a writer on exactly
    this argument — "a method with no caller and no correctness story" — and
    that judgement was right there and is being set aside here deliberately,
    so the difference is worth stating.

    A statistics writer needs a watermark column, an ordering guarantee and a
    dead-letter path, none of which exist; publishing one would have been
    publishing a *hole*. This is one operation with a complete specification:
    write the whole record, set the TTL in the same round trip, let it
    expire. There is nothing about it left to design, it is exercised by the
    tests that assert presence expires, and without it "store presence in
    Redis with a TTL" (A64-012.7) is not implemented at all — nothing would
    ever set a key, so `is_online` would be `null` for every player forever
    and the read path would be untestable against anything but a fixture.

    Its caller is AD-09's gateway. That is a wiring change in the task that
    opens the sockets, not a design change here.

    **No `clear`.** A player going offline is recorded, not erased —
    `record_presence(is_online=False)` is what makes "last seen four minutes
    ago" possible, and a delete would throw away the timestamp the record
    exists to carry. Genuine forgetting is what the TTL is for.
    """

    async def record_presence(
        self,
        player_id: UUID,
        *,
        is_online: bool,
        session_id: str | None = None,
        device_type: DeviceType | None = None,
    ) -> None:
        """Records an observation, replacing whatever was there.

        **The whole record, every time.** There is no partial write: a caller
        that knows a player is online knows when (now) and from what, so
        merging into a stored record would only ever preserve fields from an
        observation that is by definition older. Whole-record writes are also
        what make this safe across nodes — two gateways racing produce one of
        two complete records rather than a mixture of both.

        `is_online=False` is a *recorded* disconnect, not a deletion. The
        record survives for the remainder of the window carrying the instant
        the player left, which is what a profile renders as "last seen".

        **Resets the expiry on every call.** The TTL is the whole
        availability model: a node that stops writing stops asserting, and a
        node that dies mid-session leaves a record that lapses on its own
        rather than a player who is online forever. Whatever calls this must
        call it again well inside the window.

        Returns `None` and **never raises.** A failed presence write is a
        cosmetic loss, and a gateway must not drop a socket because Redis was
        briefly unreachable.
        """
        ...
