"""app.database.types.UtcDateTime — the bind/result methods are pure
functions of a value and a dialect, so they're fully unit-testable without
a database connection. tests/contract/test_mixins.py separately proves the
type round-trips through a real `timestamptz` column.
"""

from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy.engine.default import DefaultDialect

from app.database.types import UtcDateTime

_dialect = DefaultDialect()
_type = UtcDateTime()


class TestProcessBindParam:
    def test_none_passes_through(self) -> None:
        assert _type.process_bind_param(None, _dialect) is None

    def test_rejects_a_naive_datetime(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            _type.process_bind_param(datetime(2026, 1, 1), _dialect)  # noqa: DTZ001

    def test_passes_through_an_already_utc_value(self) -> None:
        value = datetime(2026, 1, 1, tzinfo=UTC)
        assert _type.process_bind_param(value, _dialect) == value

    def test_converts_a_non_utc_zone_to_utc(self) -> None:
        plus_five = timezone(timedelta(hours=5))
        value = datetime(2026, 1, 1, 5, 0, tzinfo=plus_five)
        result = _type.process_bind_param(value, _dialect)
        assert result == datetime(2026, 1, 1, 0, 0, tzinfo=UTC)


class TestProcessResultValue:
    def test_none_passes_through(self) -> None:
        assert _type.process_result_value(None, _dialect) is None

    def test_a_naive_value_from_the_driver_is_normalised_not_rejected(self) -> None:
        # Reading is defensive, not strict: a naive value coming back from
        # the driver (which should not happen against `timestamptz`, but
        # is not this type's fault if it does) is assumed UTC rather than
        # raising and breaking every read.
        result = _type.process_result_value(datetime(2026, 1, 1), _dialect)  # noqa: DTZ001
        assert result == datetime(2026, 1, 1, tzinfo=UTC)

    def test_a_non_utc_value_is_normalised_to_utc(self) -> None:
        plus_five = timezone(timedelta(hours=5))
        value = datetime(2026, 1, 1, 5, 0, tzinfo=plus_five)
        result = _type.process_result_value(value, _dialect)
        assert result == datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
