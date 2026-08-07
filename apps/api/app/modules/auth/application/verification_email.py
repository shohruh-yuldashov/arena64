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

from html import escape
from typing import Final

from app.core.enums import Locale
from app.modules.auth.domain.otp import OTP_TTL_MINUTES

_SUBJECTS: Final[dict[Locale, str]] = {
    Locale.EN: "Arena64 — your verification code",
    Locale.RU: "Arena64 — ваш код подтверждения",
    Locale.UZ: "Arena64 — tasdiqlash kodingiz",
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

    `recipient_name` is player-chosen and reaches an HTML document, so it is
    escaped at the interpolation. The text part is deliberately **not**
    escaped: `O'Brien` must read as `O'Brien` in a plain-text client, not as
    `O&#x27;Brien`.

    The code needs no escaping — it is six characters this platform
    generated from `0123456789` — and is escaped anyway, because "this value
    happens to be safe" is a property that survives until somebody changes
    the generator.
    """
    greeting = _GREETING[locale].format(name=recipient_name)
    return {
        "subject": _SUBJECTS[locale],
        "text_body": (
            f"{greeting}\n\n"
            f"{_LEAD[locale]}\n\n"
            f"    {code}\n\n"
            f"{_EXPIRY[locale]}\n\n"
            f"{_IGNORE[locale]}\n"
        ),
        "html_body": (
            '<div style="font-family:sans-serif;font-size:15px;line-height:1.5;color:#111">'
            f'<p style="margin:0 0 12px 0">{escape(greeting)}</p>'
            f'<p style="margin:0 0 12px 0">{escape(_LEAD[locale])}</p>'
            # Letter-spaced and large, because the one thing a person does
            # with this message is read six characters off it and type them
            # somewhere else. Inline styles only — a `<style>` block is
            # stripped by several major clients.
            '<p style="margin:0 0 16px 0;font-size:30px;font-weight:700;'
            f'letter-spacing:6px;font-family:monospace">{escape(code)}</p>'
            f'<p style="margin:0 0 12px 0">{escape(_EXPIRY[locale])}</p>'
            '<p style="margin:20px 0 0 0;font-size:13px;color:#666">'
            f"{escape(_IGNORE[locale])}</p>"
            "</div>"
        ),
    }


__all__ = ["build_verification_code_email"]
