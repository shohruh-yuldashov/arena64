"""The Arena64 email shell — A64-025.10E §30, redesigned in A64-025.10F §31.

One layout, used by every message this platform sends. Before it there were
three: the verification code built its own `<div>`, the notification
templates built another, and the password reset built none at all and sent
plain text only. Three shells meant three places to change a colour and one
message that had been left behind entirely.

## Why this is built out of tables

§30 called the shell "a table-free single column" and treated that as the
careful choice. It is the reason the messages arrived looking like system
output rather than like Arena64: Outlook on Windows renders mail through
Word, which has no `flex`, ignores `max-width` on a `<div>`, and will not
centre one with `margin:auto`. A centred card with a background is simply
not expressible without a layout table, so a `<div>`-only email can never
be more than left-aligned text on the client a third of business recipients
read mail in.

So the layout is tables, every one of them `role="presentation"` — that
attribute is not decoration. Without it a screen reader announces "table,
three rows" before the first sentence of every message this platform sends.

## Why the brand gradient *is* here now, and was not before

§30.2 banned it outright, on the grounds that a client which drops the
background paints white-on-white. That reasoning was right about a **bare**
gradient and wrong to conclude "never" rather than "never without something
solid beneath it". The masthead declares, in this order:

    <td bgcolor="#494fcc" style="background-color:#494fcc;
                                 background-image:linear-gradient(...)">

A client that understands none of it uses the `bgcolor` attribute; one that
understands colour but not gradients uses the declaration; one that
understands both gets the brand ramp. **Every one of those three outcomes
is white on something that clears 4.5:1** — `#494fcc` at 6.41:1 and
`#961a91` at 7.36:1 — which is the rule the product side states for text
over a gradient. `oklch` still appears nowhere: these are the same two
brand stops from `globals.css`, converted to sRGB hex once, here.

## One look, declared

`color-scheme: light` — this design is light and asks clients not to invert
it. A dark variant is honoured by some mail clients and silently ignored by
others, and a half-supported dark mode is worse than one consistent light
one. Adding it later is additive; guessing at it now is not.

## No image, ever

The wordmark is text. Mail clients block remote images by default, so a
logo is a broken-image icon at the top of the message for most first-time
recipients — and the request that would fetch it is a tracking pixel by
another name. No `<script>`, no external stylesheet, no tracking pixel.

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

# The email palette: the product's brand, converted to sRGB hex once, here.
# Hex because a mail client that meets `oklch` renders nothing.
_BRAND: Final = "#494fcc"  # --brand-from; white on it, 6.41:1
_BRAND_END: Final = "#961a91"  # --brand-to; white on it, 7.36:1
_INK: Final = "#1a1a22"
_MUTED: Final = "#6b6b7b"
_PAGE: Final = "#f2f2f7"
_CARD: Final = "#ffffff"
_LINE: Final = "#e4e4ed"
_PANEL: Final = "#f4f4fb"

# A stack, not `sans-serif`: the generic keyword resolves to Times in a few
# Windows clients, which is the one face this should never be.
_FAMILY: Final = (
    "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
)
_BODY_TEXT: Final = f"{_FAMILY};font-size:15px;line-height:1.6;color:{_INK}"

#: Padding for the card's two cells. One constant because the masthead and
#: the body must share a left edge — they are two rows of the same table and
#: a difference of a pixel is visible as a step down the message.
_GUTTER: Final = "28px"


@dataclass(frozen=True, slots=True)
class EmailAction:
    """A single call to action. Never more than one per message."""

    label: str
    url: str


def _preheader(text: str) -> str:
    """The inbox preview line, hidden inside the message.

    Every client shows ~90 characters after the subject in the message list,
    and takes them from the first text in the body. Left alone that is the
    greeting, so the most-read line of a transactional email reads "Hello
    Shohruh," in every message the platform sends.

    The run of `&#8199;&#65279;` after it is not filler: without it a client
    pulls the greeting in behind the preheader to fill the remaining width,
    which puts back the thing this removes. A figure space followed by a
    zero-width no-break space is the pair that survives whitespace
    collapsing in the widest set of clients.

    Never the verification code — `verification_email` explains why a code
    must not reach a lock screen, and a preview line is a lock screen.
    """
    return (
        '<div style="display:none;font-size:1px;line-height:1px;max-height:0;'
        'max-width:0;opacity:0;overflow:hidden;mso-hide:all">'
        f"{escape(text)}{'&#8199;&#65279;' * 60}"
        "</div>"
    )


def _masthead() -> str:
    """The brand bar. Text, on a gradient that has a solid colour beneath it."""
    return (
        f'<tr><td bgcolor="{_BRAND}" style="background-color:{_BRAND};'
        f"background-image:linear-gradient(115deg,{_BRAND},{_BRAND_END});"
        f'padding:22px {_GUTTER};border-radius:14px 14px 0 0">'
        f'<span style="{_FAMILY};font-size:19px;font-weight:700;letter-spacing:0.5px;'
        'color:#ffffff">Arena64</span>'
        "</td></tr>"
    )


def _code_panel(code: str) -> str:
    """Six digits, set to be read off a screen and typed somewhere else.

    `text-indent` cancels the trailing letter-space: `letter-spacing` adds
    its gap after the final character too, so a centred code sits half a
    space left of centre without it.

    `line-height` is stated rather than inherited. The body's is a *ratio*,
    so 30px digits inherit a 48px line box and the panel grows a third
    taller than the thing in it — with the digits sitting low in the space
    because the extra is split above and below a monospace baseline.
    """
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'border="0" style="margin:0 0 20px 0"><tr>'
        f'<td align="center" style="background-color:{_PANEL};border:1px solid {_LINE};'
        'border-radius:10px;padding:18px 12px;text-indent:8px">'
        '<span style="font-family:Menlo,Consolas,monospace;font-size:30px;font-weight:700;'
        f'line-height:1;letter-spacing:8px;color:{_INK}">{escape(code)}</span>'
        "</td></tr></table>"
    )


def _button(action: EmailAction) -> str:
    """One call to action, as a table cell rather than a padded `<a>`.

    Word's box model collapses the padding on an inline anchor, so the
    familiar `display:inline-block` button arrives in Outlook as bare
    underlined text. The colour belongs to the cell; the anchor fills it.
    """
    return (
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        'style="margin:4px 0 24px 0"><tr>'
        f'<td bgcolor="{_BRAND}" style="background-color:{_BRAND};border-radius:8px">'
        f'<a href="{escape(action.url)}" style="display:inline-block;padding:14px 26px;'
        f"{_FAMILY};font-size:15px;line-height:20px;font-weight:600;color:#ffffff;"
        f"text-decoration:none;"
        f'border-radius:8px">{escape(action.label)}</a>'
        "</td></tr></table>"
    )


def _footnote(footnote: str | None, footer: EmailAction | None) -> str:
    """The small print, above a hairline that separates it from the message."""
    link = (
        f'<br><a href="{escape(footer.url)}" style="color:{_MUTED}">{escape(footer.url)}</a>'
        if footer is not None
        else ""
    )
    return (
        f'<div style="margin:0;padding:18px 0 0 0;border-top:1px solid {_LINE};'
        f'{_FAMILY};font-size:13px;line-height:1.5;color:{_MUTED}">'
        + (escape(footnote) if footnote is not None else "")
        + link
        + "</div>"
    )


def render_email_body(
    *,
    paragraphs: Sequence[str],
    heading: str | None = None,
    preheader: str | None = None,
    code: str | None = None,
    action: EmailAction | None = None,
    footnote: str | None = None,
    footer: EmailAction | None = None,
    language: str = "en",
) -> tuple[str, str]:
    """The message, as `(text_body, html_body)`.

    `heading` is the one line that says what the message is, so the reader
    does not have to re-read the subject to find out. It is optional because
    not every message earns one: a notification whose whole body is "Round 3
    of Autumn Blitz has been published" would only be saying it twice.

    `preheader` is the inbox preview line — see `_preheader`. It appears in
    the HTML part only, because in the text part it *is* the first line and
    printing it twice is what it is there to prevent.

    `code` is set large and letter-spaced because the one thing a person
    does with a code is read it off the screen and type it somewhere else.

    `footnote` is the small grey line every one of these messages ends with
    — "ignore this if it was not you" — and `footer` is the link that
    follows it, which today is the notification preferences page. Its label
    is deliberately unused in the HTML: a reader deciding whether to trust a
    link in an email is deciding about a destination, and a label hides the
    one thing they need to see.

    `language` reaches `<html lang>`, so a screen reader pronounces a
    Russian message in Russian.

    Returns a tuple rather than an `EmailMessage`, so this function never
    sees a recipient address: one fewer place one can be logged.
    """
    text_parts: list[str] = []
    if heading is not None:
        text_parts.append(heading)
    text_parts.append("\n\n".join(paragraphs))

    content: list[str] = []
    if heading is not None:
        content.append(
            f'<h1 style="margin:0 0 14px 0;{_FAMILY};font-size:22px;line-height:1.3;'
            f'font-weight:700;color:{_INK}">{escape(heading)}</h1>'
        )
    content.append(
        "".join(
            f'<p style="margin:0 0 14px 0;{_BODY_TEXT}">{escape(line)}</p>' for line in paragraphs
        )
    )

    if code is not None:
        text_parts.append(f"    {code}")
        content.append(_code_panel(code))

    if action is not None:
        text_parts.append(f"{action.label}: {action.url}")
        content.append(_button(action))

    if footnote is not None or footer is not None:
        tail = " ".join(part for part in (footnote, footer.url if footer else None) if part)
        text_parts.append(tail)
        content.append(_footnote(footnote, footer))

    text = "\n\n".join(part for part in text_parts if part) + "\n"

    html = (
        "<!DOCTYPE html>"
        f'<html lang="{escape(language)}"><head>'
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        # Both spellings: clients moved from the second to the first and
        # some of the ones that matter still read only the old one.
        '<meta name="color-scheme" content="light">'
        '<meta name="supported-color-schemes" content="light">'
        f"</head>"
        f'<body style="margin:0;padding:0;background-color:{_PAGE};{_BODY_TEXT}">'
        + (_preheader(preheader) if preheader is not None else "")
        + f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'border="0" style="background-color:{_PAGE}"><tr>'
        '<td align="center" style="padding:32px 12px">'
        # `width` and `max-width` together: Word reads the attribute and
        # ignores the declaration, every other client does the reverse, and
        # the card has to be bounded in both.
        '<table role="presentation" width="560" cellpadding="0" cellspacing="0" border="0" '
        f'style="width:100%;max-width:560px;background-color:{_CARD};'
        f'border:1px solid {_LINE};border-radius:14px">'
        + _masthead()
        + f'<tr><td style="padding:{_GUTTER}">'
        + "".join(content)
        + "</td></tr></table></td></tr></table></body></html>"
    )
    return text, html


__all__ = ["EmailAction", "render_email_body"]
