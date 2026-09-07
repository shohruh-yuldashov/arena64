"""What a session row records about the device that opened it — A64-030.5C.

Two halves, and only the first is a security property.

**The address.** Every session on the production host recorded the nginx
container's address, because both auth routers read `request.client.host`
— the socket peer, which behind a reverse proxy is always the proxy. SE-2's
"was this me?" was answered with the same value for every device on the
platform. These tests hold `describe_device` to the trusted-proxy strategy
`app/api/rate_limiting.py` already implements: the header is read only
where a proxy is trusted, and only from the position that proxy wrote.

**The label.** Presentation, and never consulted by anything that decides.
The tests below assert the two things that matter about a parser nobody
should trust: that it puts the specific Chromium forks before the generic
families, and that an unrecognised string degrades to `None` rather than
to a confident guess.
"""

from fastapi import Request

from app.modules.auth.presentation.device import describe_device, parse_user_agent

CHROME_MACOS = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)
SAFARI_IPHONE = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/18.1 Mobile/15E148 Safari/604.1"
)
EDGE_WINDOWS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/141.0.0.0 Safari/537.36 Edg/141.0.0.0"
)
FIREFOX_LINUX = "Mozilla/5.0 (X11; Linux x86_64; rv:133.0) Gecko/20100101 Firefox/133.0"
CHROME_ANDROID = (
    "Mozilla/5.0 (Linux; Android 15; Pixel 9) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/141.0.0.0 Mobile Safari/537.36"
)


def make_request(
    *,
    client: tuple[str, int] | None = ("10.0.0.9", 51000),
    headers: dict[str, str] | None = None,
) -> Request:
    """A Starlette `Request` over a canned scope — the same shape
    `tests/unit/test_rate_limiting.py` builds, and for the same reason:
    these functions are pure, and routing them through HTTP would only
    prove that FastAPI routes."""
    raw_headers = [
        (name.lower().encode(), value.encode()) for name, value in (headers or {}).items()
    ]
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "path": "/api/v1/auth/browser/login",
            "raw_path": b"/api/v1/auth/browser/login",
            "query_string": b"",
            "headers": raw_headers,
            "client": client,
            "scheme": "https",
            "server": ("testserver", 443),
        }
    )


class TestTheRecordedAddress:
    def test_the_socket_peer_is_used_when_no_proxy_is_trusted(self) -> None:
        request = make_request(client=("203.0.113.7", 51000))

        device = describe_device(request, trusted_proxy_count=0)

        assert device.ip_address == "203.0.113.7"

    def test_a_spoofed_header_is_ignored_without_a_proxy(self) -> None:
        """**The defect this must never have.** A header any client can set
        is not an address, and storing one would put an attacker-chosen
        value in the column a player reads to recognise their own devices.
        """
        request = make_request(
            client=("203.0.113.7", 51000),
            headers={"X-Forwarded-For": "1.1.1.1"},
        )

        device = describe_device(request, trusted_proxy_count=0)

        assert device.ip_address == "203.0.113.7"

    def test_the_real_client_is_read_from_behind_one_proxy(self) -> None:
        """Production's topology: nginx replaces `X-Forwarded-For` with the
        peer it observed, and `RATE_LIMIT_TRUSTED_PROXY_COUNT` is 1."""
        request = make_request(
            client=("172.18.0.4", 51000),
            headers={"X-Forwarded-For": "203.0.113.7"},
        )

        device = describe_device(request, trusted_proxy_count=1)

        assert device.ip_address == "203.0.113.7"

    def test_the_proxys_own_address_is_never_what_is_stored(self) -> None:
        """The regression. With the count set and the header present, the
        Docker-internal address must not reach the column."""
        request = make_request(
            client=("172.18.0.4", 51000),
            headers={"X-Forwarded-For": "203.0.113.7"},
        )

        assert describe_device(request, trusted_proxy_count=1).ip_address != "172.18.0.4"

    def test_a_caller_cannot_prepend_its_way_into_the_chain(self) -> None:
        """One trusted proxy means one entry from the right. Everything to
        the left of it was supplied by the caller and is never read."""
        request = make_request(
            client=("172.18.0.4", 51000),
            headers={"X-Forwarded-For": "1.1.1.1, 203.0.113.7"},
        )

        assert describe_device(request, trusted_proxy_count=1).ip_address == "203.0.113.7"

    def test_a_missing_header_falls_back_to_the_peer(self) -> None:
        """A request that reached the application without going through the
        proxy — a health probe on the internal network — still records
        something true."""
        request = make_request(client=("172.18.0.4", 51000))

        assert describe_device(request, trusted_proxy_count=1).ip_address == "172.18.0.4"

    def test_an_unresolvable_peer_is_null_and_not_the_word_unknown(self) -> None:
        """`client_ip` answers `"unknown"` so a rate limiter has a bucket
        for every caller. A session row must not store that string: the
        column is nullable precisely so "not known" is representable, and
        a player would be shown a fake address."""
        request = make_request(client=None)

        assert describe_device(request, trusted_proxy_count=0).ip_address is None


class TestTheRecordedUserAgent:
    def test_it_is_stored_raw(self) -> None:
        request = make_request(headers={"User-Agent": CHROME_MACOS})

        assert describe_device(request, trusted_proxy_count=1).user_agent == CHROME_MACOS

    def test_an_oversized_header_is_truncated_to_the_column(self) -> None:
        """The header is attacker-controlled and lands in a column, so the
        boundary bounds it rather than the database rejecting it at flush
        time."""
        request = make_request(headers={"User-Agent": "x" * 4096})

        device = describe_device(request, trusted_proxy_count=1)

        assert device.user_agent is not None
        assert len(device.user_agent) == 512
        assert device.device_name is not None
        assert len(device.device_name) == 120

    def test_no_header_is_no_device_name(self) -> None:
        device = describe_device(make_request(), trusted_proxy_count=1)

        assert device.user_agent is None
        assert device.device_name is None


class TestTheDeviceLabel:
    def test_chrome_on_macos(self) -> None:
        label = parse_user_agent(CHROME_MACOS)

        assert (label.browser, label.platform) == ("Chrome", "macOS")

    def test_safari_on_iphone(self) -> None:
        label = parse_user_agent(SAFARI_IPHONE)

        assert (label.browser, label.platform) == ("Safari", "iPhone")

    def test_edge_is_not_reported_as_chrome(self) -> None:
        """Every Chromium fork ships `Chrome/` in its own user agent, so
        the specific tokens have to be tested before the generic ones. An
        unordered table reports this browser as Chrome."""
        label = parse_user_agent(EDGE_WINDOWS)

        assert (label.browser, label.platform) == ("Edge", "Windows")

    def test_firefox_on_linux(self) -> None:
        label = parse_user_agent(FIREFOX_LINUX)

        assert (label.browser, label.platform) == ("Firefox", "Linux")

    def test_android_is_not_reported_as_linux(self) -> None:
        """Android's user agent carries `Linux`, and a phone labelled
        "Linux" is not a device anybody recognises."""
        label = parse_user_agent(CHROME_ANDROID)

        assert (label.browser, label.platform) == ("Chrome", "Android")

    def test_an_unrecognised_agent_guesses_nothing(self) -> None:
        """`None`, not a plausible default. A confidently wrong label
        defeats the only purpose this screen has."""
        label = parse_user_agent("curl/8.5.0")

        assert (label.browser, label.platform) == (None, None)

    def test_a_known_platform_survives_an_unknown_browser(self) -> None:
        """Half an answer is still an answer: "Unknown browser on Windows"
        is true and useful, and inventing the other half would not be."""
        label = parse_user_agent("SomeNewBrowser/1.0 (Windows NT 10.0)")

        assert (label.browser, label.platform) == (None, "Windows")

    def test_no_user_agent_is_no_label(self) -> None:
        assert parse_user_agent(None) == parse_user_agent("")

    def test_a_crawler_is_not_dressed_up_as_a_device(self) -> None:
        """It would otherwise render as "Chrome on Linux" — a device the
        player never signed in on, in a list whose job is recognition."""
        label = parse_user_agent(
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(compatible; Googlebot/2.1; +http://www.google.com/bot.html) Chrome/141.0.0.0"
        )

        assert (label.browser, label.platform) == (None, None)
