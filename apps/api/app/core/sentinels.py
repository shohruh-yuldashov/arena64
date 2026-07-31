"""The `UNSET` sentinel — platform machinery for partial updates.

A `PATCH` body has three states per field, not two: *absent* (leave it
alone), *present and null* (clear it), and *present with a value* (set it).
`None` alone can only express two of them, so a service that takes
`display_name: str | None` genuinely cannot tell "don't touch this" from
"clear this" — and whichever meaning it picks is wrong half the time.

`UNSET` is the third state. It lives in `core/` rather than in the first
module that needed it because every future module's update use case has
exactly the same three-state problem, and two modules inventing two
different sentinels would be the duplication CLAUDE.md §2.1 warns about.

Used with an explicit union on an application-layer command object:

    @dataclass(frozen=True, slots=True)
    class UpdateUserProfile:
        display_name: str | None | UnsetType = UNSET

and read with `isinstance(value, UnsetType)` — never with a truthiness
check, since `UNSET`, `None`, and `""` are three distinct things here and
only the first is "absent".
"""

from typing import Any, ClassVar, Final, TypeGuard


class UnsetType:
    """The type of `UNSET`. A singleton: `UnsetType() is UNSET` holds, so
    an accidental second construction cannot produce a sentinel that fails
    an `is` comparison against the real one.
    """

    _instance: ClassVar["UnsetType | None"] = None

    def __new__(cls) -> "UnsetType":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "UNSET"

    def __bool__(self) -> bool:
        # Falsy so an accidental `if value:` at least fails closed (treats
        # it as absent) rather than silently writing the sentinel object
        # into a column. `isinstance` remains the only correct check.
        return False


UNSET: Final[UnsetType] = UnsetType()


def is_set[T](value: T | UnsetType) -> TypeGuard[T]:
    """Narrowing helper: `if is_set(cmd.display_name): ...` tells the type
    checker the value is the real type inside the branch, which a bare
    `not isinstance(..., UnsetType)` does not do as cleanly at call sites.
    """
    return not isinstance(value, UnsetType)


def unset_to_none(value: Any | UnsetType) -> Any:  # noqa: ANN401 — deliberately untyped passthrough
    """For the narrow case where "absent" and "null" genuinely mean the
    same thing to a caller. Rare on purpose — if they mean the same thing,
    the field probably should not have been three-state to begin with.
    """
    return None if isinstance(value, UnsetType) else value
