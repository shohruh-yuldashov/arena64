"""app.common.locale — Accept-Language resolution."""

import pytest

from app.common.locale import resolve_locale
from app.core.enums import DEFAULT_LOCALE, Locale


class TestResolveLocale:
    def test_no_header_returns_the_default(self) -> None:
        assert resolve_locale(None) is DEFAULT_LOCALE

    def test_empty_header_returns_the_default(self) -> None:
        assert resolve_locale("") is DEFAULT_LOCALE

    def test_exact_supported_locale(self) -> None:
        assert resolve_locale("ru") is Locale.RU

    def test_region_subtag_is_ignored(self) -> None:
        assert resolve_locale("ru-RU") is Locale.RU

    def test_is_case_insensitive(self) -> None:
        assert resolve_locale("RU") is Locale.RU

    def test_picks_the_highest_quality_supported_locale(self) -> None:
        # French isn't supported; Russian is, and is preferred over Uzbek.
        assert resolve_locale("fr;q=0.9, ru;q=0.8, uz;q=0.5") is Locale.RU

    def test_falls_back_to_default_when_nothing_is_supported(self) -> None:
        assert resolve_locale("fr-FR, de-DE;q=0.8") is DEFAULT_LOCALE

    def test_a_malformed_quality_value_is_treated_as_zero_not_fatal(self) -> None:
        assert resolve_locale("uz;q=not-a-number") is Locale.UZ

    @pytest.mark.parametrize("header", ["uz", " uz ", "uz,", ",uz"])
    def test_tolerates_stray_whitespace_and_commas(self, header: str) -> None:
        assert resolve_locale(header) is Locale.UZ
