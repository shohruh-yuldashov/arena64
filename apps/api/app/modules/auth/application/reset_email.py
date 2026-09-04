"""The password-reset message — A64-025.10E §30.

Composed here rather than inline in the service, for the reason
`verification_email` gives: `auth`'s messages are built in this layer and a
rendering package for two functions would be a package for two functions.

## What this closes

P2-2. The reset mail was **English-only and plain-text-only** while the
other two messages this platform sends were trilingual and had both parts —
composed as an f-string inside `PasswordResetService._deliver`, where
nothing about it was visible next to the messages it was inconsistent with.
The recipient's `preferred_language` was on the DTO the service already had
and was never read.

## The one sentence that does real work

"If you did not ask for this, ignore this message." A reset email arriving
unrequested is the first thing somebody sees when an attacker is probing
their account, and the correct advice is genuinely *do nothing* — saying so
stops a worried person from clicking the link to "check", which is the one
action that would consume their token for the attacker's benefit.

That sentence is the reason this message has a footnote at all, and it is
why it is stated in the reader's own language rather than in English.
"""

from typing import Final

from app.core.enums import Locale
from app.platform.email.layout import EmailAction, render_email_body

_SUBJECTS: Final[dict[Locale, str]] = {
    Locale.EN: "Reset your Arena64 password",
    Locale.RU: "Сброс пароля Arena64",
    Locale.UZ: "Arena64 parolini tiklash",
}

# The heading, which is not the subject: a subject competes for room in a
# list beside twenty others and says which product it is, while a heading is
# read inside a message whose masthead already answered that.
_HEADING: Final[dict[Locale, str]] = {
    Locale.EN: "Reset your password",
    Locale.RU: "Сброс пароля",
    Locale.UZ: "Parolni tiklash",
}

_GREETING: Final[dict[Locale, str]] = {
    Locale.EN: "Hello {name},",
    Locale.RU: "Здравствуйте, {name}!",
    Locale.UZ: "Assalomu alaykum, {name}!",
}

_LEAD: Final[dict[Locale, str]] = {
    Locale.EN: (
        "Somebody asked to reset the password on your Arena64 account. "
        "If it was you, choose a new one:"
    ),
    Locale.RU: (
        "Кто-то запросил сброс пароля для вашего аккаунта Arena64. "
        "Если это были вы, задайте новый пароль:"
    ),
    Locale.UZ: (
        "Kimdir Arena64 hisobingiz parolini tiklashni so'radi. "
        "Agar bu siz bo'lsangiz, yangi parol tanlang:"
    ),
}

_ACTION: Final[dict[Locale, str]] = {
    Locale.EN: "Choose a new password",
    Locale.RU: "Задать новый пароль",
    Locale.UZ: "Yangi parol tanlash",
}

# Plural forms are avoided rather than solved: the TTL is configurable and
# Russian would need one/few/many for it. "{hours} h" is not a sentence, so
# each language states the duration in a shape that stays correct for any
# number — which is what a settings-driven value forces.
_EXPIRY: Final[dict[Locale, str]] = {
    Locale.EN: (
        "The link works for {hours} hours and can be used once. "
        "Resetting your password signs you out on every device."
    ),
    Locale.RU: (
        "Ссылка действует {hours} ч. и срабатывает один раз. "
        "Смена пароля завершает сеансы на всех устройствах."
    ),
    Locale.UZ: (
        "Havola {hours} soat amal qiladi va bir marta ishlaydi. "
        "Parolni almashtirish barcha qurilmalardagi seanslarni tugatadi."
    ),
}

_IGNORE: Final[dict[Locale, str]] = {
    Locale.EN: (
        "If you did not ask for this, ignore this message. "
        "Your password has not changed and no action is needed."
    ),
    Locale.RU: (
        "Если вы этого не запрашивали, просто проигнорируйте письмо. "
        "Пароль не изменился, делать ничего не нужно."
    ),
    Locale.UZ: (
        "Agar buni siz so'ramagan bo'lsangiz, xatni e'tiborsiz qoldiring. "
        "Parolingiz o'zgarmadi va hech narsa qilish shart emas."
    ),
}


def build_password_reset_email(
    *, reset_url: str, recipient_name: str, locale: Locale, ttl_hours: int
) -> dict[str, str]:
    """The message, as `EmailMessage`'s keyword arguments.

    A mapping rather than an `EmailMessage`, so the caller supplies the
    recipient and this function never sees an address.
    """
    text_body, html_body = render_email_body(
        heading=_HEADING[locale],
        # The lead rather than the greeting — A64-025.10F §31. Left alone,
        # the inbox preview of this message reads "Assalomu alaykum,
        # Shohruh!", which is the one line a reader sees before deciding
        # whether an unrequested reset needs their attention.
        preheader=_LEAD[locale],
        paragraphs=[
            _GREETING[locale].format(name=recipient_name),
            _LEAD[locale],
        ],
        action=EmailAction(label=_ACTION[locale], url=reset_url),
        footnote=f"{_EXPIRY[locale].format(hours=ttl_hours)} {_IGNORE[locale]}",
        language=locale.value,
    )
    return {"subject": _SUBJECTS[locale], "text_body": text_body, "html_body": html_body}


__all__ = ["build_password_reset_email"]
