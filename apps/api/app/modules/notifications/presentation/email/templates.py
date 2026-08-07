"""Notification email, rendered — A64-021.5 §13, §14, §15, §16, §17.

Presentation, and deliberately in `presentation/` rather than `domain/`: a
subject line is a rendering of a fact, in a language, for a mail client. The
fact is `NotificationRecord`, and it does not change when the copy does.

## Rendered at send time, never stored

§13. A body frozen at enqueue time would be sent in whatever locale the
recipient had *then*, and would keep being sent after a template fixed a
mistake in it. Rendering from the typed payload means a retry produces the
current template in the current language.

## Every user-controlled string is escaped, once, here

A tournament name is chosen by an operator and a display name by a player,
and both reach an HTML document. `escape` is applied at the **interpolation
site** rather than at the boundary, because that is the only place that
knows a value is about to become markup — the text part must *not* be
escaped, or a tournament called "Bob & Sons" reads as `Bob &amp; Sons` in
every plain-text client.

There is no template engine and no `format` on a caller-supplied string.
Every template below is a Python function, so there is no string a payload
could contain that becomes a placeholder.

## Both parts, always

§17: a transactional email is never HTML-only. `render` returns both, and
`EmailMessage` carries both — a client that refuses markup, a screen reader
and a plain-text archive all get a complete message.

## The HTML is deliberately boring

A table-free single column, inline styles only, no `<script>`, no external
stylesheet, no image, no tracking pixel (§17). Mail clients strip most of
what a web page can do, and the parts they keep are the parts that break
differently in each of them.
"""

from collections.abc import Mapping
from html import escape
from typing import Final

from app.core.enums import Locale
from app.modules.notifications.domain.email import RenderedEmail
from app.modules.notifications.domain.record import (
    NotificationRecord,
    NotificationType,
    TournamentSummary,
)


class UnsupportedEmailTemplate(LookupError):
    """No template for this notification type.

    A `LookupError` rather than a returned `None`, because the caller has
    already asked `supports_email` and reaching this means the two
    disagreed — a defect rather than an outcome. The delivery service
    records it as an unsupported type and does not retry.
    """


#: The one link every notification email carries.
#:
#: A **path**, joined to the configured public origin at render time. Never a
#: full URL in a template and never one from a payload: §14's rule, and the
#: reason is that a stored or event-supplied URL is an open redirect with a
#: mail client in front of it.
def _tournament_path(tournament_id: str) -> str:
    return f"/tournaments/{tournament_id}"


# --- copy ------------------------------------------------------------------
#
# Transactional and short, per §15. No marketing, no salutation beyond a
# name, no unsubscribe prose beyond the one link §18 asks for. Final tone is
# Product Experience Redesign's, and this is deliberately plain enough that
# rewriting it later touches nothing else.

_SUBJECTS: Final[Mapping[NotificationType, Mapping[Locale, str]]] = {
    NotificationType.TOURNAMENT_REGISTRATION_CONFIRMED: {
        Locale.EN: "Arena64 — Tournament registration confirmed",
        Locale.RU: "Arena64 — Регистрация на турнир подтверждена",
        Locale.UZ: "Arena64 — Turnirga ro'yxatdan o'tish tasdiqlandi",
    },
    NotificationType.TOURNAMENT_ROUND_PUBLISHED: {
        Locale.EN: "Arena64 — Your next tournament round is ready",
        Locale.RU: "Arena64 — Следующий тур турнира готов",
        Locale.UZ: "Arena64 — Turnirning navbatdagi turi tayyor",
    },
    NotificationType.TOURNAMENT_COMPLETED: {
        Locale.EN: "Arena64 — Tournament completed",
        Locale.RU: "Arena64 — Турнир завершён",
        Locale.UZ: "Arena64 — Turnir yakunlandi",
    },
}

_CALL_TO_ACTION: Final[Mapping[NotificationType, Mapping[Locale, str]]] = {
    NotificationType.TOURNAMENT_REGISTRATION_CONFIRMED: {
        Locale.EN: "Open tournament",
        Locale.RU: "Открыть турнир",
        Locale.UZ: "Turnirni ochish",
    },
    NotificationType.TOURNAMENT_ROUND_PUBLISHED: {
        Locale.EN: "Open tournament",
        Locale.RU: "Открыть турнир",
        Locale.UZ: "Turnirni ochish",
    },
    NotificationType.TOURNAMENT_COMPLETED: {
        Locale.EN: "View results",
        Locale.RU: "Посмотреть результаты",
        Locale.UZ: "Natijalarni ko'rish",
    },
}

_PREFERENCE_NOTE: Final[Mapping[Locale, str]] = {
    Locale.EN: "You can change which notifications you receive in your settings:",
    Locale.RU: "Вы можете изменить получаемые уведомления в настройках:",
    Locale.UZ: "Qaysi bildirishnomalarni olishingizni sozlamalarda o'zgartirishingiz mumkin:",
}


def _lines(record: NotificationRecord, locale: Locale) -> list[str]:
    """The body, as sentences, **unescaped**.

    One list per message, shared by both parts: the text renderer joins it
    and the HTML renderer escapes each line into a paragraph. Composing the
    two independently is how they drift, and a plain-text body that says
    something different from the markup is worse than either alone.
    """
    payload = record.payload
    if not isinstance(payload, TournamentSummary):  # pragma: no cover — see below
        # Unreachable while `EMAIL_CAPABLE_TYPES` holds only tournament
        # types, and kept because the two are separate declarations: a type
        # added to that set without a template here should fail loudly at
        # render rather than send an empty message.
        raise UnsupportedEmailTemplate(f"no email body for {record.type}")

    name = payload.tournament_name
    if record.type is NotificationType.TOURNAMENT_REGISTRATION_CONFIRMED:
        return {
            Locale.EN: [f"You are registered for {name}."],
            Locale.RU: [f"Вы зарегистрированы на турнир {name}."],
            Locale.UZ: [f"Siz {name} turniriga ro'yxatdan o'tdingiz."],
        }[locale]

    if record.type is NotificationType.TOURNAMENT_ROUND_PUBLISHED:
        round_number = payload.round_number
        return {
            Locale.EN: [f"Round {round_number} of {name} has been published."],
            Locale.RU: [f"Тур {round_number} турнира {name} опубликован."],
            Locale.UZ: [f"{name} turnirining {round_number}-turi e'lon qilindi."],
        }[locale]

    if record.type is NotificationType.TOURNAMENT_COMPLETED:
        finished = {
            Locale.EN: f"{name} has completed.",
            Locale.RU: f"Турнир {name} завершён.",
            Locale.UZ: f"{name} turniri yakunlandi.",
        }[locale]
        if payload.final_rank is None:
            # No standing recorded — a player who withdrew before the field
            # was fixed. The tournament ended, which is true; inventing a
            # placement is not.
            return [finished]
        rank = {
            Locale.EN: f"Your final rank: {payload.final_rank}.",
            Locale.RU: f"Ваше итоговое место: {payload.final_rank}.",
            Locale.UZ: f"Sizning yakuniy o'rningiz: {payload.final_rank}.",
        }[locale]
        return [finished, rank]

    raise UnsupportedEmailTemplate(f"no email body for {record.type}")


class TemplateEmailRenderer:
    """`application.ports.NotificationEmailRenderer` — the templates below.

    Holds the **configured** frontend origin, validated at settings
    construction to be a bare scheme-and-host. Every link this renders is
    that origin plus a path built from an identifier — there is no branch
    that concatenates a caller-supplied string into a URL, so no payload can
    produce a scheme, a host, or a `javascript:` target.

    A class rather than a function with an argument, because the origin is
    process configuration: passing it per call would mean every caller held
    it, and the one that forgot would render links to nowhere.
    """

    def __init__(self, *, public_origin: str) -> None:
        self._public_origin = public_origin

    def render(self, record: NotificationRecord, *, locale: Locale) -> RenderedEmail:
        """One notification as a message. Raises `UnsupportedEmailTemplate`."""
        return render(record, locale=locale, public_origin=self._public_origin)


def render(
    record: NotificationRecord,
    *,
    locale: Locale,
    public_origin: str,
) -> RenderedEmail:
    """The rendering itself, as a free function.

    Separate from the class so a test can render one message without
    constructing a renderer, and so the origin is visible at the one call
    site that supplies it.
    """
    subject = _SUBJECTS.get(record.type, {}).get(locale)
    if subject is None:
        raise UnsupportedEmailTemplate(f"no email subject for {record.type}")

    payload = record.payload
    if not isinstance(payload, TournamentSummary):  # pragma: no cover
        raise UnsupportedEmailTemplate(f"no email body for {record.type}")

    lines = _lines(record, locale)
    action_url = f"{public_origin}{_tournament_path(str(payload.tournament_id))}"
    action_label = _CALL_TO_ACTION[record.type][locale]
    # §18: the preference screen, not a tokenised one-click unsubscribe. A
    # token in a URL is a bearer credential in a mailbox, and this platform
    # has a settings page behind a session that does the same job.
    settings_url = f"{public_origin}/settings/notifications"

    return RenderedEmail(
        subject=subject,
        text_body=_as_text(lines, action_label, action_url, settings_url, locale),
        html_body=_as_html(lines, action_label, action_url, settings_url, locale),
    )


def _as_text(
    lines: list[str],
    action_label: str,
    action_url: str,
    settings_url: str,
    locale: Locale,
) -> str:
    """The plain-text part. **Nothing is escaped here.**

    Escaping text that is never parsed as markup is how `Bob & Sons` becomes
    `Bob &amp; Sons` in somebody's inbox — a bug that only shows up in the
    half of the message nobody looks at during review.
    """
    body = "\n".join(lines)
    return f"{body}\n\n{action_label}: {action_url}\n\n{_PREFERENCE_NOTE[locale]}\n{settings_url}\n"


def _as_html(
    lines: list[str],
    action_label: str,
    action_url: str,
    settings_url: str,
    locale: Locale,
) -> str:
    """The HTML part. Every interpolation escaped, at the interpolation.

    The URLs are escaped too, and that is not belt-and-braces: they contain
    a UUID and a configured origin today, and the day one carries a query
    parameter an unescaped `&` is a malformed document in a client that
    parses strictly.

    Inline styles only, and few of them. A `<style>` block is stripped by
    several major clients and a `class` attribute by more, so anything that
    matters has to survive as an attribute on the element it styles.
    """
    paragraphs = "".join(f'<p style="margin:0 0 12px 0">{escape(line)}</p>' for line in lines)
    return (
        '<div style="font-family:sans-serif;font-size:15px;line-height:1.5;color:#111">'
        f"{paragraphs}"
        f'<p style="margin:20px 0"><a href="{escape(action_url)}" '
        'style="display:inline-block;padding:10px 16px;background:#111;color:#fff;'
        'text-decoration:none;border-radius:6px">'
        f"{escape(action_label)}</a></p>"
        '<p style="margin:24px 0 0 0;font-size:13px;color:#666">'
        f"{escape(_PREFERENCE_NOTE[locale])} "
        f'<a href="{escape(settings_url)}" style="color:#666">{escape(settings_url)}</a>'
        "</p>"
        "</div>"
    )


__all__ = ["TemplateEmailRenderer", "UnsupportedEmailTemplate", "render"]
