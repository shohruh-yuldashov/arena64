"""What a request says about the device behind it — A64-030.5C.

Two questions, both asked at the HTTP boundary and nowhere else:

    describe_device      what a new session row records
    parse_user_agent     what the device list shows a player

They live together because they are two halves of one contract. The first
decides what is *stored*; the second decides how the stored value is *read
back*. Splitting them would let the truncation and the parser disagree
about what a `user_agent` column holds.

## Why this file exists at all

`router.py` and `browser_router.py` each carried a private `_device_of`,
byte-identical, and both of them recorded the wrong address in production.

`request.client.host` is the socket peer. Behind the nginx edge every
socket peer is the edge, so every session row on the production host
recorded a Docker-internal address — SE-2's "was this me?" answered with
the same value for every device on the platform.

The fix is not a new mechanism. `app/api/rate_limiting.py::client_ip`
already implements this deployment's trusted-proxy strategy, already reads
`RATE_LIMIT_TRUSTED_PROXY_COUNT`, and is already the number
`nginx/snippets/proxy.conf` was written against. A second implementation
would be a second thing to get wrong; this calls the first one.

## Parsing is presentation, and is never a decision

`parse_user_agent` produces two display strings and is read by nothing
that authorises, rate-limits, or branches on identity. A `User-Agent` is
attacker-controlled and trivially forged, so a rule that consulted it
would be a rule with an opt-out.

It is deliberately **not** a dependency. `ua-parser` and its siblings
carry a regex corpus with a monthly update cadence, and the whole of what
is needed here is a browser family and an operating system family for the
sentence "Chrome on macOS". Forty lines of ordered `in` tests answer that
for every client this platform sees, degrade to `None` rather than
guessing, and cannot be wrong in a way that matters — the caller renders
"Unknown browser" and the player still recognises their own device by the
platform and the timestamp beside it.

`None` is a real answer here and is preserved all the way to the wire. A
parser that invented "Chrome" for an unrecognised string would produce a
device list that is confidently wrong, which SE-2 is explicit is worse
than one that is plainly incomplete.

## Parsed on read, not on write

The label is computed from the stored `user_agent` when the list is
served, rather than baked into `device_name` at sign-in. Two consequences,
both wanted: improving the parser improves every existing row, and a
session created before this file existed still gets a label.
"""

import re
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request

from app.api.deps import SettingsDep
from app.api.rate_limiting import UNKNOWN_CLIENT, client_ip
from app.modules.auth.domain.sessions import SessionDevice
from app.modules.auth.infrastructure.models import (
    DEVICE_NAME_MAX_LENGTH,
    USER_AGENT_MAX_LENGTH,
)


def describe_device(request: Request, *, trusted_proxy_count: int) -> SessionDevice:
    """What this request's session row records about its device.

    Both strings are truncated to their column widths here, at the
    boundary, because the header is attacker-controlled and lands in a
    column: a 40 KB `User-Agent` must be a short row rather than a
    `DataError` at flush time.

    `device_name` keeps the raw truncated header rather than a parsed
    label — see the module docstring on why the label is computed on read.
    """
    user_agent = request.headers.get("user-agent")
    return SessionDevice(
        device_name=user_agent[:DEVICE_NAME_MAX_LENGTH] if user_agent else None,
        user_agent=user_agent[:USER_AGENT_MAX_LENGTH] if user_agent else None,
        ip_address=_address_of(request, trusted_proxy_count=trusted_proxy_count),
    )


def _address_of(request: Request, *, trusted_proxy_count: int) -> str | None:
    """The caller's address, or `None` when it cannot be determined.

    `client_ip` answers the literal string `"unknown"` for a request with
    no resolvable peer, because a rate limiter needs every caller in some
    bucket. A session row needs the opposite: `ip_address` is nullable
    precisely so that "not known" is storable, and writing `"unknown"`
    into it would put a fake address in a column a player is shown.
    """
    address = client_ip(request, trusted_proxy_count=trusted_proxy_count)
    return address if address != UNKNOWN_CLIENT else None


@dataclass(frozen=True, slots=True)
class DeviceLabel:
    """How a device is named to the player who owns it.

    Two fields rather than one sentence, because the sentence is
    localised. "Chrome on macOS" and "Chrome — macOS" are the same data in
    two languages, and composing it here would ship English word order to
    every locale. The browser and the platform are product names and are
    the same in all three.
    """

    browser: str | None = None
    platform: str | None = None


#: Ordered, and the order is the whole correctness of the table.
#:
#: Every Chromium fork ships "Chrome" in its own user agent, and Safari's
#: token appears in almost all of them, so a first match against an
#: unordered set would report Edge, Opera and Samsung Internet as Chrome
#: and report Chrome as Safari. The specific forks are therefore tested
#: first and the generic families last.
_BROWSERS: tuple[tuple[str, str], ...] = (
    ("Edg/", "Edge"),
    ("EdgiOS/", "Edge"),
    ("OPR/", "Opera"),
    ("Opera", "Opera"),
    ("SamsungBrowser", "Samsung Internet"),
    ("YaBrowser", "Yandex Browser"),
    ("Vivaldi", "Vivaldi"),
    ("Brave", "Brave"),
    # Firefox on iOS is `FxiOS`; it is Firefox to the person holding it,
    # whatever engine Apple obliges it to use.
    ("FxiOS/", "Firefox"),
    ("Firefox/", "Firefox"),
    ("CriOS/", "Chrome"),
    ("Chromium/", "Chromium"),
    ("Chrome/", "Chrome"),
    # Last, and only with `Version/`: bare "Safari" is the token every
    # WebKit-derived agent carries, and matching it earlier would relabel
    # most of the table above.
    ("Version/", "Safari"),
)

#: Also ordered. `iPhone` and `iPad` both carry "Mac OS X" in the same
#: string, and Android carries "Linux".
_PLATFORMS: tuple[tuple[str, str], ...] = (
    ("iPhone", "iPhone"),
    ("iPad", "iPad"),
    ("Android", "Android"),
    ("Windows NT", "Windows"),
    ("CrOS", "ChromeOS"),
    ("Mac OS X", "macOS"),
    ("Macintosh", "macOS"),
    ("Linux", "Linux"),
)

#: A bot says so, and saying "Chrome on Linux" about one is a device list
#: that names something the player never signed in on. Checked before the
#: tables, and answers `DeviceLabel()` — the same graceful nothing an
#: unrecognised agent gets.
_ROBOT = re.compile(r"bot|crawler|spider|crawling|headless", re.IGNORECASE)


def parse_user_agent(user_agent: str | None) -> DeviceLabel:
    """The browser and platform families in a user agent, as far as they
    can honestly be told.

    Never raises, never guesses, and never returns a partial guess for one
    field to fill the other: a string that names a platform and no
    recognised browser answers with the platform alone, which renders as
    "Unknown browser on Windows" and is true.
    """
    if not user_agent or _ROBOT.search(user_agent):
        return DeviceLabel()

    return DeviceLabel(
        browser=_first_match(user_agent, _BROWSERS),
        platform=_first_match(user_agent, _PLATFORMS),
    )


def _first_match(haystack: str, table: tuple[tuple[str, str], ...]) -> str | None:
    for token, name in table:
        if token in haystack:
            return name
    return None


def get_session_device(request: Request, settings: SettingsDep) -> SessionDevice:
    """`describe_device` as a dependency, so the proxy count is resolved
    once rather than at each call site.

    A dependency and not a module-level helper taking `Request` alone,
    because the trust boundary is *configuration* — `RATE_LIMIT_TRUSTED_PROXY_COUNT`
    — and a handler that reached for it itself is a handler that can
    forget to. Every route creating a session takes this, so there is no
    path that records an address without going through the boundary.
    """
    return describe_device(request, trusted_proxy_count=settings.rate_limit.trusted_proxy_count)


SessionDeviceDep = Annotated[SessionDevice, Depends(get_session_device)]


__all__ = [
    "DeviceLabel",
    "SessionDeviceDep",
    "describe_device",
    "get_session_device",
    "parse_user_agent",
]
