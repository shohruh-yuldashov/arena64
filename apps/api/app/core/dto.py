"""Base DTO classes — the boundary types services.md's interface layer
maps domain results into and out of.

Two bases, not one, because inbound and outbound have opposite correctness
requirements (services.md §6 Tier 1, and §3.3's prohibition on services
accepting transport models applies symmetrically to what they return):

  request DTOs   must reject anything they don't recognise — an unknown
                 field is far more often a client bug (a stale build, a
                 typo) than a forwards-compatible extension, and silently
                 dropping it hides that from the caller.
  response DTOs  must be constructible directly from a domain entity or
                 ORM row (`from_attributes`), so the interface layer maps
                 `Match -> MatchResponseDTO` without hand-copying every
                 field, and must never be validated as if they were
                 untrusted input — they're built from data this service
                 already trusts.

Neither base defines any field: a DTO with no module to describe yet would
just be inventing shape (CLAUDE.md §1 rule 7). The first module to add a
schema subclasses one of these two.
"""

from pydantic import BaseModel, ConfigDict


class BaseRequestDTO(BaseModel):
    """A wire-format request body or query parameters. Strict on purpose —
    services.md §6 Tier 1: transport validation rejects before any I/O, and
    an unrecognised field rejected here can never reach tier 3 as silently
    mis-typed input.
    """

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        frozen=True,
    )


class BaseResponseDTO(BaseModel):
    """A wire-format response body, built from a domain entity or ORM row.

    `from_attributes=True` is what lets the interface layer write
    `MatchResponseDTO.model_validate(match)` instead of naming every field
    twice. Not frozen: a response DTO is assembled by the interface layer
    (setting fields, computing a derived one) before being returned, unlike
    a request DTO, which arrives complete or not at all.
    """

    model_config = ConfigDict(from_attributes=True)
