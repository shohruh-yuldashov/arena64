"""The edge configuration and the application must agree — A64-028.6A §17–§20.

`infrastructure/production/nginx/` decides three things the application
cannot: which paths are private, which paths the SPA serves, and what a
Content-Security-Policy allows. All three are stated in files no compiler
reads, and all three fail **silently** when they drift:

  - a route added to the router and not to the edge returns 404 on a deep
    link while working perfectly in development;
  - a private route added to the router and not marked `noindex` becomes
    indexable, and nobody finds out until it is in a search result;
  - an inline script edited in `index.html` invalidates its CSP hash, and the
    theme flashes on first paint for every visitor.

A fourth arrived with nginx and has no counterpart in the Caddy
configuration this replaced: `add_header` is **not inherited** into a
location that declares one of its own. A location that adds a
`Cache-Control` silently loses every security header set above it. That is
the failure `TestHeaderInheritance` exists for.

These are static checks against the files themselves. They need no network
and no Docker: the live HTTP behaviour is proven separately, in the
deployment verification the task's report carries. The two are not
redundant — A64-028.6 shipped a Caddy configuration that passed every static
assertion and returned an empty 200 for every private route, because the
list of paths was right and the routing semantics were not.
"""

import hashlib
import re
from base64 import b64encode
from pathlib import Path

import pytest

from app.platform.metrics.prometheus import NAMESPACE  # noqa: F401 — see _series_in

_REPO = Path(__file__).resolve().parents[4]
NGINX = _REPO / "infrastructure" / "production" / "nginx"
MAIN_CONF = NGINX / "nginx.conf"
APP_HOST = NGINX / "templates" / "10-arena64.conf.template"
ADMIN_HOST = NGINX / "templates" / "20-admin.conf.template"
REDIRECT = NGINX / "conf.d" / "00-redirect.conf"
HEADERS_COMMON = NGINX / "snippets" / "headers-common.conf"
HEADERS_APP = NGINX / "snippets" / "headers-app.conf"
HEADERS_ADMIN = NGINX / "snippets" / "headers-admin.conf"
WEBSOCKET = NGINX / "snippets" / "websocket.conf"
PROXY = NGINX / "snippets" / "proxy.conf"
TLS = NGINX / "snippets" / "tls.conf"

ROUTES = _REPO / "apps" / "web" / "src" / "app" / "router" / "routes.tsx"
ROBOTS = _REPO / "apps" / "web" / "public" / "robots.txt"
INDEX_HTML = _REPO / "apps" / "web" / "index.html"


#: A `location` block's header line and body, for every block in a file.
def _csp(snippet: Path) -> str:
    """The Content-Security-Policy header's **value**, from the directive."""
    found = re.search(r'add_header Content-Security-Policy "([^"]+)"', _directives(snippet))
    assert found is not None, f"{snippet.name} declares no CSP"
    return found.group(1)


_LOCATION = re.compile(
    r"^\s*location\s+(?P<match>[^{]+?)\s*\{(?P<body>.*?)^\s*\}",
    re.MULTILINE | re.DOTALL,
)


def _directives(path: Path) -> str:
    """A file with its comments removed.

    Every one of these configurations explains itself at length, and the
    prose names the directives it is justifying — `ssl_ciphers`,
    `$proxy_add_x_forwarded_for`, `'unsafe-inline'`. A test searching the
    raw text finds the explanation and reports the thing being explained as
    present.

    Four assertions in this file failed that way on their first run. The
    same mistake was made against the Caddyfile in A64-028.6 and fixed the
    same way; it is written down here so the third time is caught by
    reading rather than by running.
    """
    return "\n".join(
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )


def _locations(path: Path) -> list[tuple[str, str]]:
    """Every `location` block as (matcher, body).

    Regex rather than a real parser because the shape is narrow and known:
    these files are written by this repository and every block is one level
    deep. A nested block would break this, and a nested block is not
    something this configuration has any reason to grow.
    """
    return [
        (found.group("match").strip(), found.group("body"))
        for found in _LOCATION.finditer(path.read_text(encoding="utf-8"))
    ]


def _serves_the_shell(body: str) -> bool:
    return "try_files /index.html" in body


def _router_paths() -> set[str]:
    """Every `path:` the web router declares, with parameters generalised.

    TanStack states parameters as `$name`. A parameterised segment cannot be
    enumerated at the edge, so the edge matches its parent by prefix and this
    reduces such a route to that parent — which is exactly what has to be
    present for the deep link to resolve.
    """
    declared = re.findall(r'path:\s*"([^"]+)"', ROUTES.read_text(encoding="utf-8"))
    return {path[: path.index("$")].rstrip("/") if "$" in path else path for path in declared}


def _matches(pattern: str, path: str) -> bool:
    """Whether one nginx `location` matcher matches a path.

    Covers the three forms this configuration uses — `=` exact, `^~` and
    bare prefix, and `~` regex — because a test that only understood one of
    them would quietly pass on routes served by the others.
    """
    pattern = pattern.strip()
    if pattern.startswith("= "):
        return path == pattern[2:].strip()
    if pattern.startswith("~ "):
        return re.search(pattern[2:].strip(), path) is not None
    if pattern.startswith("^~ "):
        return path.startswith(pattern[3:].strip())
    return path.startswith(pattern)


class TestEveryRouteIsServed:
    def test_the_edge_serves_every_route_the_router_declares(self) -> None:
        """A route the edge does not know returns a real 404 — which is the
        point of §17 and is also how a working page disappears."""
        shell = [matcher for matcher, body in _locations(APP_HOST) if _serves_the_shell(body)]

        missing = {
            path
            for path in _router_paths()
            if not any(_matches(matcher, path) for matcher in shell)
        }

        assert not missing, (
            f"routes the production edge would 404: {sorted(missing)}. "
            "Add them to infrastructure/production/nginx/templates/10-arena64.conf.template."
        )

    @pytest.mark.parametrize("unknown", ["/gmaes/abc", "/settings/nope", "/friends/nope", "/nope"])
    def test_an_unknown_path_reaches_no_shell_location(self, unknown: str) -> None:
        """The mirror, and the one a catch-all `try_files $uri /index.html`
        would break: every URL on the internet would be a page."""
        shell = [matcher for matcher, body in _locations(APP_HOST) if _serves_the_shell(body)]

        assert not any(_matches(matcher, unknown) for matcher in shell), (
            f"{unknown} would be served the application shell with 200"
        )


class TestPrivateRoutesAreNotIndexable:
    """`robots.txt` asks a crawler not to fetch. `X-Robots-Tag` tells it not
    to index, which is the half that binds for a URL discovered from a link.
    """

    def test_every_disallowed_path_carries_noindex(self) -> None:
        disallowed = {
            line.removeprefix("Disallow:").strip()
            for line in ROBOTS.read_text(encoding="utf-8").splitlines()
            if line.startswith("Disallow:") and line.removeprefix("Disallow:").strip()
        }
        noindex = [
            matcher
            for matcher, body in _locations(APP_HOST)
            if "X-Robots-Tag" in body and "noindex" in body
        ]

        shell = [matcher for matcher, body in _locations(APP_HOST) if _serves_the_shell(body)]

        # The invariant is "not indexable", which two outcomes satisfy: a
        # location that sets the header, or a path the edge does not serve
        # at all — a 404, and the 404 page carries `noindex` itself
        # (asserted by `TestHeaderInheritance`).
        #
        # Both are needed. `robots.txt` disallows the `/settings/` subtree
        # while the edge enumerates its five real children, so
        # `/settings/anything-else` is correctly a 404 rather than a page
        # somebody forgot to mark.
        def indexable(path: str) -> bool:
            probe = f"{path}probe" if path.endswith("/") else path
            if any(_matches(matcher, probe) for matcher in noindex):
                return False
            return any(_matches(matcher, probe) for matcher in shell)

        missing = {path for path in disallowed if indexable(path)}

        assert not missing, (
            f"paths robots.txt disallows but the edge would let be indexed: {sorted(missing)}"
        )

    def test_the_landing_page_is_not_marked_noindex(self) -> None:
        """The mirror of the test above, and the more expensive mistake.

        A private route that stays indexable is a leak; the one indexable
        page carrying `noindex` is the whole product missing from search,
        and nothing in the application would report it.
        """
        for matcher, body in _locations(APP_HOST):
            if matcher.strip() == "= /":
                assert "X-Robots-Tag" not in body
                return
        pytest.fail("the landing page has no `location = /` block")

    def test_the_admin_host_is_noindex_wholesale(self) -> None:
        """Every route on that host is private, so the header belongs in the
        snippet every one of its locations includes rather than on each."""
        assert "X-Robots-Tag" in _directives(HEADERS_ADMIN)


class TestTheOperatorSurfaceIsNotPublic:
    @pytest.mark.parametrize("path", ["/metrics", "/health/drain"])
    @pytest.mark.parametrize("host", [APP_HOST, ADMIN_HOST], ids=["product", "admin"])
    def test_it_is_refused_at_the_edge(self, host: Path, path: str) -> None:
        """The second of the two boundaries — the first is the bearer token
        in `app/api/security.py`. Either alone has a way of being wrong.

        Asserted per host: both declare the rule, and a whole-file search
        would keep passing after one of them had stopped refusing.
        """
        refusing = [
            matcher for matcher, body in _locations(host) if re.search(r"return\s+404", body)
        ]

        assert any(_matches(matcher, path) for matcher in refusing), (
            f"{path} is not refused on {host.name}"
        )

    def test_the_websocket_is_not_caught_by_the_metrics_rule(self) -> None:
        """`^~ /metrics` is a prefix and `/ws` is exact; a regression that
        broadened either would take the realtime path down."""
        refusing = [
            matcher for matcher, body in _locations(APP_HOST) if re.search(r"return\s+404", body)
        ]

        assert not any(_matches(matcher, "/ws") for matcher in refusing)


class TestHeaderInheritance:
    """The nginx-specific failure, and the one with no Caddy counterpart.

    `add_header` is not merged down a location tree: a block declaring one of
    its own **discards every inherited one**. A location that adds a
    `Cache-Control` therefore loses HSTS, the CSP and the rest, silently, and
    the only way to notice is to look at a response.
    """

    @pytest.mark.parametrize(
        ("host", "snippet"),
        [(APP_HOST, "headers-app.conf"), (ADMIN_HOST, "headers-admin.conf")],
        ids=["product", "admin"],
    )
    def test_every_location_that_adds_a_header_includes_the_snippet(
        self, host: Path, snippet: str
    ) -> None:
        naked = [
            matcher
            for matcher, body in _locations(host)
            if "add_header" in body and snippet not in body
        ]

        assert not naked, (
            f"locations in {host.name} that set a header without including {snippet}, "
            f"and therefore serve no security headers at all: {naked}"
        )

    @pytest.mark.parametrize("host", [APP_HOST, ADMIN_HOST], ids=["product", "admin"])
    def test_the_error_pages_carry_the_headers_too(self, host: Path) -> None:
        """A 4xx is the response an attacker can most easily provoke, so it
        is the one a framing or sniffing protection matters most on."""
        error_pages = [(matcher, body) for matcher, body in _locations(host) if "internal;" in body]

        assert error_pages, f"{host.name} declares no internal error page"
        for matcher, body in error_pages:
            assert "headers-" in body, f"{matcher} in {host.name} serves no security headers"

    def test_every_directive_in_the_snippets_is_always(self) -> None:
        """Without `always`, `add_header` applies to a handful of 2xx and 3xx
        codes only — so the headers would be absent from exactly the
        responses that need them."""
        for snippet in (HEADERS_COMMON, HEADERS_APP, HEADERS_ADMIN):
            for line in _directives(snippet).splitlines():
                if line.strip().startswith("add_header"):
                    assert line.rstrip().endswith("always;"), f"{snippet.name}: {line.strip()}"


class TestTheContentSecurityPolicy:
    def test_it_carries_a_hash_for_every_inline_script(self) -> None:
        """An inline script whose hash is not in the policy does not run.

        The theme script runs before first paint precisely so there is no
        flash of the wrong theme; a stale hash brings the flash back for
        every visitor, on every first load, with nothing in any log.
        """
        html = INDEX_HTML.read_text(encoding="utf-8")
        policy = _csp(HEADERS_APP)
        for body in re.findall(r"<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)</script>", html):
            digest = b64encode(hashlib.sha256(body.encode("utf-8")).digest()).decode()
            assert f"'sha256-{digest}'" in policy, (
                "an inline script in apps/web/index.html has no matching hash in the "
                "production CSP — recompute it and update headers-app.conf."
            )

    def test_scripts_are_not_allowed_inline_wholesale(self) -> None:
        """`'unsafe-inline'` in `script-src` would make the hashes above
        decoration. It is permitted for styles, and the snippet says why."""
        policy = _csp(HEADERS_APP)
        directive = policy[policy.index("script-src") : policy.index("style-src")]
        assert "'unsafe-inline'" not in directive

    @pytest.mark.parametrize(
        "directive",
        ["default-src 'self'", "object-src 'none'", "frame-ancestors 'none'", "base-uri 'self'"],
    )
    def test_the_baseline_directives_are_present_on_both_hosts(self, directive: str) -> None:
        assert directive in _csp(HEADERS_APP)
        assert directive in _csp(HEADERS_ADMIN)

    def test_nothing_may_be_evaluated(self) -> None:
        for snippet in (HEADERS_APP, HEADERS_ADMIN):
            assert "unsafe-eval" not in _csp(snippet)

    def test_no_directive_uses_a_wildcard_source(self) -> None:
        """A `*` anywhere in a policy is a policy that stopped being one."""
        for snippet in (HEADERS_APP, HEADERS_ADMIN):
            assert " *" not in _csp(snippet)


class TestTransportHeaders:
    @pytest.mark.parametrize(
        "header",
        [
            "Strict-Transport-Security",
            "X-Content-Type-Options",
            "Referrer-Policy",
            "Permissions-Policy",
        ],
    )
    def test_it_is_set(self, header: str) -> None:
        assert header in HEADERS_COMMON.read_text(encoding="utf-8")


class TestTheRealtimePath:
    def test_the_websocket_location_is_the_path_the_router_declares(self) -> None:
        """`/ws`, read from `app/gateway/router.py` rather than assumed —
        an edge proxying a guessed path is an edge with no realtime."""
        declared = (_REPO / "apps" / "api" / "app" / "gateway" / "router.py").read_text(
            encoding="utf-8"
        )
        path = re.search(r'@gateway_router\.websocket\("([^"]+)"\)', declared)
        assert path is not None

        matchers = [matcher for matcher, body in _locations(APP_HOST) if "websocket.conf" in body]
        assert any(_matches(matcher, path.group(1)) for matcher in matchers)

    def test_the_upgrade_headers_are_set(self) -> None:
        """`Connection` is hop-by-hop, so nginx drops it unless told
        otherwise — and the handshake then returns 200 with a broken socket
        rather than an error, which is the hardest failure to diagnose."""
        websocket = _directives(WEBSOCKET)
        assert "proxy_set_header Upgrade $http_upgrade;" in websocket
        assert "proxy_set_header Connection $connection_upgrade;" in websocket
        assert "proxy_http_version 1.1;" in websocket

    def test_buffering_is_off_for_the_socket(self) -> None:
        """A buffered stream is a stream that arrives when the buffer fills."""
        assert "proxy_buffering off;" in _directives(WEBSOCKET)

    def test_the_read_timeout_outlives_a_quiet_game(self) -> None:
        """`proxy_read_timeout` counts silence, and a game between moves is
        silent. The 30s default would sever a socket during most pauses."""
        assert re.search(r"proxy_read_timeout\s+1h;", _directives(WEBSOCKET))


class TestForwardedHeaderTrust:
    def test_the_forwarded_for_header_is_replaced_and_not_appended(self) -> None:
        """`$proxy_add_x_forwarded_for` appends to whatever the client sent.

        Replacing means a spoofed value cannot survive under **any**
        `RATE_LIMIT_TRUSTED_PROXY_COUNT`, rather than being safe only while
        that number and the proxy chain agree. It is what the Caddy
        configuration did, and it is strictly the safer of the two.
        """
        proxy = _directives(PROXY)
        assert "proxy_set_header X-Forwarded-For $remote_addr;" in proxy
        assert "$proxy_add_x_forwarded_for" not in proxy

    def test_the_websocket_path_replaces_it_too(self) -> None:
        """The socket carries the same identity as a request and is rate
        limited on it. A gap here would be a bypass on the one path that
        stays open for the length of a game."""
        websocket = _directives(WEBSOCKET)
        assert "proxy_set_header X-Forwarded-For $remote_addr;" in websocket
        assert "$proxy_add_x_forwarded_for" not in websocket


class TestTlsPolicy:
    def test_obsolete_protocols_are_not_offered(self) -> None:
        tls = _directives(TLS)
        protocols = re.search(r"ssl_protocols ([^;]+);", tls)
        assert protocols is not None
        offered = set(protocols.group(1).split())
        assert offered == {"TLSv1.2", "TLSv1.3"}

    def test_no_hand_written_cipher_list(self) -> None:
        """A 40-entry cipher string freezes a judgement against an OpenSSL
        that keeps moving, and every such string eventually names something
        that has since been broken."""
        assert "ssl_ciphers" not in _directives(TLS)


class TestTheHttpListener:
    def test_the_acme_challenge_is_not_redirected(self) -> None:
        """The challenge is served over plain HTTP by definition — there is
        no certificate yet. Redirecting it makes first issuance impossible
        and every renewal after it."""
        challenge = [
            matcher
            for matcher, body in _locations(REDIRECT)
            if "acme-challenge" in matcher and "return 301" not in body
        ]
        assert challenge, "the ACME challenge path is missing or redirected"

    def test_everything_else_is_redirected(self) -> None:
        redirects = [
            matcher for matcher, body in _locations(REDIRECT) if "return 301 https://" in body
        ]
        assert any(matcher.strip() == "/" for matcher in redirects)


class TestCachePolicy:
    def test_hashed_assets_are_immutable(self) -> None:
        for matcher, body in _locations(APP_HOST):
            if matcher.strip() == "^~ /assets/":
                assert "immutable" in body
                return
        pytest.fail("no /assets/ location")

    @pytest.mark.parametrize("shell", ["= /", "= /index.html", "= /sw.js"])
    def test_the_shell_is_never_immutable(self, shell: str) -> None:
        """A deploy changes what `index.html` points at. Serving it from an
        immutable cache pins every returning visitor to the previous
        deploy's assets, which are gone."""
        for matcher, body in _locations(APP_HOST):
            if matcher.strip() == shell:
                assert "immutable" not in body
                assert "no-cache" in body
                return
        pytest.fail(f"no `location {shell}` block")
