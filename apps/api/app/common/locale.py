"""Locale resolution — `Accept-Language` parsing, shared by anything that
needs to know which of the platform's three supported locales
(`app.core.enums.Locale`) a caller prefers: a future notification renderer
choosing a template locale (database.md DB-25), or any other
server-generated text.

A technical utility with a real dependency (the inbound `Request`), which
is why it lives in `common/` and not `core/` (dependency-injection.md
§3.3) — `Locale` itself is the framework-free type this resolves to.
"""

from typing import Annotated

from fastapi import Depends, Request

from app.core.enums import DEFAULT_LOCALE, Locale


def resolve_locale(accept_language: str | None) -> Locale:
    """Parses a raw `Accept-Language` header (RFC 9110 §12.5.4, simplified:
    quality values order preferences but are not otherwise weighted) and
    returns the caller's most-preferred *supported* locale, or
    `DEFAULT_LOCALE` if none of their preferences are supported.
    """
    if not accept_language:
        return DEFAULT_LOCALE

    candidates: list[tuple[float, str]] = []
    for raw_part in accept_language.split(","):
        part = raw_part.strip()
        if not part:
            continue

        tag, _, params = part.partition(";")
        quality = 1.0
        for raw_param in params.split(";"):
            param = raw_param.strip()
            if param.startswith("q="):
                try:
                    quality = float(param.removeprefix("q="))
                except ValueError:
                    quality = 0.0

        # A tag may carry a region ("ru-RU"); only the primary subtag
        # matters, since none of the platform's locales are region-specific.
        primary = tag.strip().split("-")[0].lower()
        candidates.append((quality, primary))

    for _, primary in sorted(candidates, key=lambda candidate: candidate[0], reverse=True):
        try:
            return Locale(primary)
        except ValueError:
            continue

    return DEFAULT_LOCALE


def get_locale(request: Request) -> Locale:
    """FastAPI dependency form of `resolve_locale`. Not consumed by any
    route yet — this task builds platform infrastructure only, and no
    module renders locale-dependent output yet — but every future one that
    does depends on exactly this, built once here rather than reimplemented
    per module.
    """
    return resolve_locale(request.headers.get("accept-language"))


LocaleDep = Annotated[Locale, Depends(get_locale)]
