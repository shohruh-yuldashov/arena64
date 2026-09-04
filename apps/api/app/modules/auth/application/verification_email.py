"""The verification-code message — A64-021.5H §12.

One template, three languages, both parts. Composed here rather than in
`presentation/` because `auth`'s two existing messages are composed in their
service too: this module has no rendering layer and inventing one for six
digits would be a package for one function.

## What the code is, and is not, allowed to touch

**Never the subject line, never a URL, never a query parameter.** A subject
is displayed by every notification surface a phone has — a lock screen, a
watch, a preview pane — and a code visible without unlocking anything is a
code a shoulder-surfer reads. A URL carrying it lands in browser history, in
a proxy log and in a referrer header.

So the subject says what the message is and the body says the code, which is
the same rule a bank card PIN letter follows and for the same reason.

## No call to action

There is deliberately no button and no link. The person is already on the
page that is waiting for the code — the message exists to move six digits
across an air gap, and a link would invite them to leave the tab that has
their half-finished session in it.

That also removes the one thing a phishing copy of this email would need.
"""

from typing import Final

from app.core.enums import Locale
from app.modules.auth.domain.otp import OTP_TTL_MINUTES
from app.platform.email.layout import render_email_body

_SUBJECTS: Final[dict[Locale, str]] = {
    Locale.EN: "Arena64 — your verification code",
    Locale.RU: "Arena64 — ваш код подтверждения",
    Locale.UZ: "Arena64 — tasdiqlash kodingiz",
}

_HEADING: Final[dict[Locale, str]] = {
    Locale.EN: "Your verification code",
    Locale.RU: "Ваш код подтверждения",
    Locale.UZ: "Tasdiqlash kodingiz",
}

_GREETING: Final[dict[Locale, str]] = {
    Locale.EN: "Hello {name},",
    Locale.RU: "Здравствуйте, {name}!",
    Locale.UZ: "Assalomu alaykum, {name}!",
}

_LEAD: Final[dict[Locale, str]] = {
    Locale.EN: "Your Arena64 verification code is:",
    Locale.RU: "Ваш код подтверждения Arena64:",
    Locale.UZ: "Arena64 tasdiqlash kodingiz:",
}

_EXPIRY: Final[dict[Locale, str]] = {
    Locale.EN: f"The code expires in {OTP_TTL_MINUTES} minutes.",
    Locale.RU: f"Код действителен {OTP_TTL_MINUTES} минут.",
    Locale.UZ: f"Kod {OTP_TTL_MINUTES} daqiqa amal qiladi.",
}

_IGNORE: Final[dict[Locale, str]] = {
    Locale.EN: "If you did not request this, you can ignore this message.",
    Locale.RU: "Если вы не запрашивали код, просто проигнорируйте это письмо.",
    Locale.UZ: "Agar buni siz so'ramagan bo'lsangiz, bu xatni e'tiborsiz qoldiring.",
}


def build_verification_code_email(
    *, code: str, recipient_name: str, locale: Locale
) -> dict[str, str]:
    """The message, as `EmailMessage`'s keyword arguments.

    Returns a mapping rather than an `EmailMessage` so the caller supplies
    the recipient — this function never sees an address, which is one fewer
    place one can be logged.

    Escaping is `render_email_body`'s — A64-025.10E §30 moved this message
    onto the shared shell, so `O'Brien` still reaches a plain-text client as
    `O'Brien` and an HTML one safely, and neither rule is restated here.
    """
    text_body, html_body = render_email_body(
        heading=_HEADING[locale],
        # The lead, and **never** the code. An inbox preview is displayed by
        # every notification surface a phone has, which is the whole reason
        # the code is kept out of the subject — a preheader carrying it
        # would put it back on the lock screen through the other door.
        preheader=_LEAD[locale],
        paragraphs=[
            _GREETING[locale].format(name=recipient_name),
            _LEAD[locale],
        ],
        code=code,
        footnote=f"{_EXPIRY[locale]} {_IGNORE[locale]}",
        language=locale.value,
    )
    return {"subject": _SUBJECTS[locale], "text_body": text_body, "html_body": html_body}


__all__ = ["build_verification_code_email"]
