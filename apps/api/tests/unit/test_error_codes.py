"""app.core.error_codes — the registry stays in sync with the taxonomy."""

from app.core.error_codes import ErrorCode
from app.core.exceptions import Arena64Error


def _all_subclasses(cls: type[Arena64Error]) -> set[type[Arena64Error]]:
    subclasses = set(cls.__subclasses__())
    return subclasses.union(s for c in subclasses for s in _all_subclasses(c))


def test_every_exception_class_default_code_is_a_registered_error_code() -> None:
    every_class: set[type[Arena64Error]] = {Arena64Error, *_all_subclasses(Arena64Error)}
    for klass in every_class:
        assert isinstance(klass.default_code, ErrorCode), (
            f"{klass.__name__}.default_code is not an ErrorCode member"
        )


def test_error_code_values_are_stable_wire_strings() -> None:
    # These are a public contract with apps/web/src/types/api.ts, which
    # mirrors this enum by hand — a renamed value here is a breaking change
    # that must be made deliberately, not discovered by a client 500ing.
    assert ErrorCode.NOT_FOUND.value == "not_found"
    assert ErrorCode.VALIDATION_ERROR.value == "validation_error"
    assert ErrorCode.INTERNAL_ERROR.value == "internal_error"
