"""The Arena64 email shell — A64-025.10E §30.

One layout, used by every message this platform sends. Before it there were
three: the verification code built its own `<div>`, the notification
templates built another, and the password reset built none at all and sent
plain text only. Three shells meant three places to change a colour and one
message that had been left behind entirely.

## Why the brand gradient is not here

`globals.css` gives the product a gradient and A64-025.9B rations it to
three places. **None of them is an email.** Gradients on a button are
unevenly supported across mail clients, `oklch` is not supported at all, and
a client that drops the background paints white-on-white text — a button
nobody can read is worse than a plain one. So the palette here is a flat,
boring translation of the same brand, in hex, and it is written down as a
deliberate divergence rather than an oversight.

The same reasoning the notification templates already recorded applies to
everything else: a table-free single column, inline styles only, no
`<script>`, no external stylesheet, no image, no tracking pixel.

## Both parts, always

A transactional email is never HTML-only. Every function here returns both,
and the text part is **never escaped** — escaping a string that is never
parsed as markup is how `Bob & Sons` reaches an inbox as `Bob &amp; Sons`,
in the half of the message nobody reads during review.

Escaping happens at the interpolation site in the HTML part, because that is
the only place that knows a value is about to become markup.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from html import escape
from typing import Final

# The email palette. Hex, because a mail client that meets `oklch` renders
# nothing; flat, because a gradient that fails renders nothing readable.
_INK: Final = "#111"
_MUTED: Final = "#666"
_FONT: Final = "font-family:sans-serif;font-size:15px;line-height:1.5"


@dataclass(frozen=True, slots=True)
class EmailAction:
    """A single call to action. Never more than one per message."""

    label: str
    url: str


def render_email_body(
    *,
    paragraphs: Sequence[str],
    code: str | None = None,
    action: EmailAction | None = None,
    footnote: str | None = None,
    footer: EmailAction | None = None,
) -> tuple[str, str]:
    """The message body, as `(text_body, html_body)`.

    `code` is set large and letter-spaced because the one thing a person
    does with a code is read it off the screen and type it somewhere else.

    `footnote` is the small grey line every one of these messages ends with
    — "ignore this if it was not you" — and `footer` is the link that
    follows it, which today is the notification preferences page.

    Returns a tuple rather than an `EmailMessage`, so this function never
    sees a recipient address: one fewer place one can be logged.
    """
    text_parts: list[str] = ["\n\n".join(paragraphs)]
    html_parts: list[str] = [
        "".join(f'<p style="margin:0 0 12px 0">{escape(line)}</p>' for line in paragraphs)
    ]

    if code is not None:
        text_parts.append(f"    {code}")
        html_parts.append(
            '<p style="margin:0 0 16px 0;font-size:30px;font-weight:700;'
            f'letter-spacing:6px;font-family:monospace">{escape(code)}</p>'
        )

    if action is not None:
        text_parts.append(f"{action.label}: {action.url}")
        html_parts.append(
            f'<p style="margin:20px 0"><a href="{escape(action.url)}" '
            f'style="display:inline-block;padding:10px 16px;background:{_INK};color:#fff;'
            'text-decoration:none;border-radius:6px">'
            f"{escape(action.label)}</a></p>"
        )

    if footnote is not None or footer is not None:
        tail = " ".join(part for part in (footnote, footer.url if footer else None) if part)
        text_parts.append(tail)
        html_parts.append(
            f'<p style="margin:24px 0 0 0;font-size:13px;color:{_MUTED}">'
            + (escape(footnote) if footnote is not None else "")
            + (
                f' <a href="{escape(footer.url)}" style="color:{_MUTED}">{escape(footer.url)}</a>'
                if footer is not None
                else ""
            )
            + "</p>"
        )

    text = "\n\n".join(part for part in text_parts if part) + "\n"
    html = f'<div style="{_FONT};color:{_INK}">' + "".join(html_parts) + "</div>"
    return text, html


__all__ = ["EmailAction", "render_email_body"]
