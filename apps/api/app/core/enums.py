"""Shared enums — platform-wide, business-free (contrast with a module's
own domain enums, e.g. a future `game.TerminationReason`, which belong in
that module's `domain/`, not here). A type belongs in this file only once
more than one future module would otherwise redefine it.
"""

from enum import StrEnum


class Locale(StrEnum):
    """The three locales the platform supports (task A64-008; mirrors
    `apps/web/src/i18n/routing.ts`'s `routing.locales` and anticipates
    `database.md`'s `reference.locale` table). `EN` is first because it is
    the default — `app.common.locale.resolve_locale` falls back to it.
    """

    EN = "en"
    RU = "ru"
    UZ = "uz"


DEFAULT_LOCALE: Locale = Locale.EN


class SortDirection(StrEnum):
    """Ascending or descending — the one property every orderable listing
    needs, regardless of what it lists or how it's paginated
    (`app.core.pagination`).
    """

    ASC = "asc"
    DESC = "desc"
