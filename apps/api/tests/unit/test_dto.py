"""app.core.dto — base request/response boundary types."""

import pytest
from pydantic import ValidationError as PydanticValidationError

from app.core.dto import BaseRequestDTO, BaseResponseDTO


class _CreateThingRequest(BaseRequestDTO):
    name: str


class _Thing:
    """A plain object standing in for a domain entity or ORM row."""

    def __init__(self, name: str) -> None:
        self.name = name


class _ThingResponse(BaseResponseDTO):
    name: str


class TestBaseRequestDTO:
    def test_rejects_an_unrecognised_field(self) -> None:
        with pytest.raises(PydanticValidationError):
            _CreateThingRequest.model_validate({"name": "a", "unexpected": "field"})

    def test_strips_surrounding_whitespace(self) -> None:
        dto = _CreateThingRequest.model_validate({"name": "  padded  "})
        assert dto.name == "padded"

    def test_is_frozen(self) -> None:
        dto = _CreateThingRequest(name="a")
        with pytest.raises(PydanticValidationError):
            dto.name = "b"  # type: ignore[misc]


class TestBaseResponseDTO:
    def test_constructs_directly_from_a_non_dict_object(self) -> None:
        # The point of `from_attributes`: no domain entity or ORM row ever
        # needs to be dict-ified before crossing the interface boundary.
        dto = _ThingResponse.model_validate(_Thing(name="widget"))
        assert dto.name == "widget"
