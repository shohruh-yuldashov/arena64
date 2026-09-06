"""The edge configuration and the application must agree — A64-028.6 §14–16.

`infrastructure/production/Caddyfile` decides three things the application
cannot: which paths are private, which paths the SPA serves, and what a
Content-Security-Policy allows. All three are stated in a file no compiler
reads and no test would otherwise touch, and all three fail **silently** when
they drift:

  - a route added to the router and not to `@spa` returns 404 on a deep link
    while working perfectly in development;
  - a private route added to the router and not to `@private` becomes
    indexable, and nobody finds out until it is in a search result;
  - an inline script edited in `index.html` invalidates its CSP hash, and the
    theme flashes on first paint for every visitor.

These are static checks against the files themselves. They need no network
and no Docker: the live HTTP behaviour is proven separately, in the
deployment verification the task's report carries.
"""

import hashlib
import re
from base64 import b64encode
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[4]
CADDYFILE = _REPO / "infrastructure" / "production" / "Caddyfile"
ROUTES = _REPO / "apps" / "web" / "src" / "app" / "router" / "routes.tsx"
ROBOTS = _REPO / "apps" / "web" / "public" / "robots.txt"
INDEX_HTML = _REPO / "apps" / "web" / "index.html"

PRODUCT_HOST = "{$ARENA64_DOMAIN} {"
ADMIN_HOST = "admin.{$ARENA64_DOMAIN} {"


def _caddyfile() -> str:
    return CADDYFILE.read_text(encoding="utf-8")


def _host(host: str) -> str:
    """One site block's text.

    Scoped rather than whole-file, and it took a mutation to find out why:
    both hosts declare an `@operator` matcher, so a whole-file search still
    found `/metrics` after the product host had stopped refusing it — the
    test passed on a configuration that served the exporter to the internet.
    """
    text = _caddyfile()
    start = text.index(f"\n{host}")
    remainder = text[start + 1 :]
    end = remainder.find("\nadmin.")
    return remainder if end == -1 else remainder[:end]


def _block(name: str, host: str = PRODUCT_HOST) -> set[str]:
    """The paths one named matcher matches, in either of Caddy's two forms.

    A matcher is written inline (`@api path /api/*`) or as a braced block of
    `path` lines, and both appear in this file. Reading every `path` in the
    file instead would silently union `@private`, `@spa`, `@api` and
    `@operator` — and a test that cannot tell them apart would pass on a
    configuration that serves the metrics endpoint to the internet.
    """
    text = _host(host)
    paths: set[str] = set()
    for match in re.finditer(rf"^\s*@{name}\s+(.*)$", text, re.MULTILINE):
        body = match.group(1).strip()
        if body.startswith("{"):
            continue
        if body.startswith("path "):
            paths.update(body.removeprefix("path ").split())
    for match in re.finditer(rf"^\s*@{name} {{$", text, re.MULTILINE):
        end = text.index("\n\t}", match.end())
        for line in text[match.end() : end].splitlines():
            if line.strip().startswith("path "):
                paths.update(line.strip().removeprefix("path ").split())
    return paths


def _policy(host: str) -> str:
    """One host's Content-Security-Policy header **value**.

    Parsed from the directive rather than sliced out of the file, because
    the file explains the policy in a comment directly above it — and a
    naive slice picked up the prose, which mentions every directive it is
    justifying.
    """
    text = _caddyfile()
    start = text.index(host)
    match = re.search(r'header Content-Security-Policy "([^"]+)"', text[start:])
    assert match is not None, f"no CSP for {host}"
    return match.group(1)


def _router_paths() -> set[str]:
    """Every `path:` the web router declares, as an edge-shaped pattern.

    TanStack states parameters as `$name`; Caddy matches a prefix with `*`.
    A parameterised segment therefore becomes a `/*` suffix on its parent,
    which is exactly what the Caddyfile has to contain for the deep link to
    resolve.
    """
    declared = re.findall(r'path:\s*"([^"]+)"', ROUTES.read_text(encoding="utf-8"))
    patterns: set[str] = set()
    for path in declared:
        if "$" in path:
            patterns.add(path[: path.index("$")].rstrip("/") + "/*")
        else:
            patterns.add(path)
    return patterns


class TestEveryRouteIsServed:
    def test_the_spa_matcher_covers_the_router(self) -> None:
        """A route the edge does not know returns a real 404 — which is the
        point of §16 and is also how a working page disappears."""
        missing = {
            path
            for path in _router_paths()
            if path not in _block("spa") and _parent_of(path) not in _block("spa")
        }

        assert not missing, (
            f"routes the production edge would 404: {sorted(missing)}. "
            "Add them to the @spa matcher in infrastructure/production/Caddyfile."
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
        private = _block("private")

        missing = set()
        for path in disallowed:
            if path.startswith("/api"):
                # Served by the @api handler, which imports no_index itself.
                continue
            candidates = {path, path.rstrip("/"), path.rstrip("/") + "/*", path + "*"}
            if not candidates & private:
                missing.add(path)

        assert not missing, (
            f"paths robots.txt disallows but the edge would let be indexed: {sorted(missing)}"
        )

    def test_the_landing_page_is_not_marked_noindex(self) -> None:
        """The mirror of the test above, and the more expensive mistake.

        A private route that stays indexable is a leak; the one indexable
        page carrying `noindex` is the whole product missing from search,
        and nothing in the application would report it.
        """
        assert "/" not in _block("private")

    def test_the_admin_host_is_noindex_wholesale(self) -> None:
        assert "import no_index" in _host(ADMIN_HOST)


class TestTheOperatorSurfaceIsNotPublic:
    @pytest.mark.parametrize("path", ["/metrics", "/health/drain"])
    @pytest.mark.parametrize("host", [PRODUCT_HOST, ADMIN_HOST])
    def test_it_is_refused_at_the_edge(self, host: str, path: str) -> None:
        """The second of the two boundaries — the first is the bearer token
        in `app/api/security.py`. Either alone has a way of being wrong.

        Asserted per host: both declare the matcher, and a whole-file search
        kept passing after one of them had stopped refusing.
        """
        assert path in _block("operator", host)


class TestTheContentSecurityPolicy:
    def test_it_carries_a_hash_for_every_inline_script(self) -> None:
        """An inline script whose hash is not in the policy does not run.

        The theme script runs before first paint precisely so there is no
        flash of the wrong theme; a stale hash brings the flash back for
        every visitor, on every first load, with nothing in any log.
        """
        html = INDEX_HTML.read_text(encoding="utf-8")
        policy = _policy(PRODUCT_HOST)
        for body in re.findall(r"<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)</script>", html):
            digest = b64encode(hashlib.sha256(body.encode("utf-8")).digest()).decode()
            assert f"'sha256-{digest}'" in policy, (
                "an inline script in apps/web/index.html has no matching hash in the "
                "production CSP — recompute it and update the Caddyfile."
            )

    def test_scripts_are_not_allowed_inline_wholesale(self) -> None:
        """`'unsafe-inline'` in `script-src` would make the hashes above
        decoration. It is permitted for styles, and the Caddyfile says why."""
        policy = _policy(PRODUCT_HOST)
        script_src = policy[policy.index("script-src") : policy.index("style-src")]
        assert "'unsafe-inline'" not in script_src

    @pytest.mark.parametrize(
        "directive",
        ["default-src 'self'", "object-src 'none'", "frame-ancestors 'none'", "base-uri 'self'"],
    )
    def test_the_baseline_directives_are_present(self, directive: str) -> None:
        assert directive in _policy(PRODUCT_HOST)
        assert directive in _policy(ADMIN_HOST)

    def test_nothing_may_be_evaluated(self) -> None:
        assert "unsafe-eval" not in _caddyfile()


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
        assert header in _caddyfile()


def _parent_of(path: str) -> str:
    """`/settings/profile` → `/settings/*`, so a nested route is covered by
    its parent's prefix matcher rather than needing its own line."""
    head, _, _ = path.rstrip("/").rpartition("/")
    return f"{head}/*" if head else path
