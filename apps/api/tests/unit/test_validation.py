"""app.core.validation — reusable Annotated types and validators."""

from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError

from app.core.validation import NonEmptyStr, UtcDatetime, ensure_utc


class _Named(BaseModel):
    name: NonEmptyStr


class _Scheduled(BaseModel):
    at: UtcDatetime


class TestNonEmptyStr:
    def test_accepts_a_normal_value(self) -> None:
        assert _Named(name="Arena64").name == "Arena64"

    def test_rejects_an_empty_string(self) -> None:
        with pytest.raises(PydanticValidationError):
            _Named(name="")

    def test_rejects_a_whitespace_only_string(self) -> None:
        with pytest.raises(PydanticValidationError):
            _Named(name="   ")

    def test_strips_surrounding_whitespace(self) -> None:
        assert _Named(name="  padded  ").name == "padded"


class TestEnsureUtc:
    def test_rejects_a_naive_datetime(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            ensure_utc(datetime(2026, 1, 1))  # noqa: DTZ001 — the point of this test

    def test_passes_through_an_already_utc_datetime(self) -> None:
        value = datetime(2026, 1, 1, tzinfo=UTC)
        assert ensure_utc(value) == value

    def test_converts_a_non_utc_zone_to_utc(self) -> None:
        plus_five = timezone(timedelta(hours=5))
        value = datetime(2026, 1, 1, 5, 0, tzinfo=plus_five)
        assert ensure_utc(value) == datetime(2026, 1, 1, 0, 0, tzinfo=UTC)


class TestUtcDatetimeAnnotation:
    def test_rejects_naive_input_through_pydantic(self) -> None:
        with pytest.raises(PydanticValidationError, match="timezone-aware"):
            _Scheduled(at=datetime(2026, 1, 1))  # noqa: DTZ001

    def test_normalises_a_non_utc_zone_through_pydantic(self) -> None:
        plus_five = timezone(timedelta(hours=5))
        scheduled = _Scheduled(at=datetime(2026, 1, 1, 5, 0, tzinfo=plus_five))
        assert scheduled.at == datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
