# SEO and Public Discoverability

| Field            | Value                                         |
| ---------------- | --------------------------------------------- |
| **Status**       | Implemented                                   |
| **Owner**        | Shohruh                                       |
| **Applies to**   | `apps/web`                                    |
| **Decided in**   | A64-026.3 — `specs/product-experience.md` §42 |
| **Last updated** | 2026-09-05                                    |

---

## 1. What this covers

Which URLs a search engine may crawl, what Arena64 says about itself in a
document head, and where those values come from. Values are not repeated
here where they have an owner: the brand tokens are `globals.css`'s and the
copy is the landing page's.

## 2. Indexing policy

**One page on this origin is meant to be found.**

| Route                                                                                                                   | Crawlable       | Why                                                                                                                         |
| ----------------------------------------------------------------------------------------------------------------------- | --------------- | --------------------------------------------------------------------------------------------------------------------------- |
| `/` (anonymous landing)                                                                                                 | **Yes**         | The only page written to be read by somebody without an account                                                             |
| `/login`, `/register`, `/verify-email`, `/forgot-password`, `/reset-password`                                           | No              | Forms. A search result landing a stranger on a password reset helped nobody                                                 |
| `/players/{username}`                                                                                                   | **No** — see §3 | Public to view, not for indexing                                                                                            |
| `/profile`, `/settings/*`, `/friends`, `/challenges`, `/search`, `/play`, `/games/*`, `/notifications`, `/tournaments*` | No              | Behind `protectedPage`. A crawler reaching one is redirected to sign-in, so the only thing it could index is the login form |
| `/api/*`                                                                                                                | No              | Shares the origin in production. Nothing under it is a document                                                             |

The list lives in `apps/web/public/robots.txt` with the reasoning inline, and
`src/app/seo.test.ts` asserts every row of it.

### `Disallow` is not `noindex`

`Disallow` stops crawling. A disallowed URL can still be **listed** if
something links to it — as a bare URL with no description. Closing that gap
needs an `X-Robots-Tag: noindex` response header, which the host sends and
this bundle cannot.

A per-route `<meta name="robots">` cannot substitute: every route in this
SPA serves the same `index.html`, so a meta tag written for one route is
written for all of them, and the one that matters most (`/`) must stay
indexable. **This is a deployment requirement, recorded in §7.**

## 3. Player profiles: public to view, not for indexing

`/players/{username}` is open — the server filters what a viewer sees and
turns nobody away. It is nevertheless disallowed.

The privacy settings a player has today control **who sees what**. None of
them says _"and list me in a search engine"_. Indexing a person on a
permission they were never asked for is the wrong default, and it is the one
that cannot be taken back: removal from an index takes months, while adding
an opt-in later costs a setting.

If profile indexing is ever wanted it needs a **new, explicit preference**,
defaulting off, and a per-profile `noindex` that only a server can send.

## 4. Canonical URLs and the origin

Everything absolute needs one value this repository cannot know:
`VITE_PUBLIC_ORIGIN`.

|          |                                                                         |
| -------- | ----------------------------------------------------------------------- |
| Frontend | `VITE_PUBLIC_ORIGIN` — read by `scripts/generate-seo.mjs` at build time |
| Backend  | `PUBLIC_APP_URL` — already existed, used for mailed links               |

**They must be the same string in a deployment.** One is what an emailed
reset link points at and the other is what a canonical claims; a site whose
mail and whose canonical disagree about its own address has two identities.

`https` is required for anything but `localhost`, and a trailing slash is
dropped, so there is one spelling of every URL the site publishes about
itself. Both are enforced, not documented-and-hoped.

### A build with no origin does not get indexed

If `VITE_PUBLIC_ORIGIN` is unset, `robots.txt` is **replaced with
`Disallow: /`** and no canonical, sitemap or structured data is written.

That is the safe reading: a build that cannot say which URL its content
belongs to is a preview, a staging deploy or somebody's laptop. It also
means forgetting the variable **fails closed** rather than publishing a
canonical built from `localhost`, which would tell a crawler to index a page
that does not exist.

## 5. Metadata

Static in `apps/web/index.html`, because `/` is the landing page and this
application has no per-route metadata layer (§6).

|               |                                                                                                                         |
| ------------- | ----------------------------------------------------------------------------------------------------------------------- |
| `<title>`     | Says what the product is, not just its name                                                                             |
| `description` | Real capabilities, no keyword stuffing                                                                                  |
| OpenGraph     | `type`, `site_name`, `title`, `description`, `image`, `locale`, plus `url` at build time                                |
| Twitter       | `summary_large_image`, because there is a real 1200×630 card                                                            |
| Social image  | `public/og-card.png`, generated by `npm run assets:og` from this app's own stylesheet — A64-026.2 §41.4                 |
| Language      | Uzbek, matching `<html lang>`. A crawler and a share preview each read one value, before any script has chosen a locale |

### Structured data

One `WebSite` and one `WebApplication`, injected at build time because `url`
is most of what makes them useful.

**Nothing else.** No `aggregateRating`, `review`, `offers`, `author` or
`interactionCount` — none has a source, and a rich result built on an
invented rating is a manual action against the domain rather than a nicer
listing. A test forbids each of those strings by name.

## 6. The SPA limit, measured

`apps/web` is client-rendered. A production build's `index.html` has a
complete `<head>` and a **body containing zero characters of text** —
measured, not assumed.

| Consumer                                        | Sees                                                                                                                                                       |
| ----------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Google                                          | The rendered page — it executes JavaScript                                                                                                                 |
| Link previews (Telegram, WhatsApp, Facebook, X) | **The head only.** They do not execute JavaScript — which is why the OG tags are static and correct for `/`, and why per-route OG tags would be decorative |
| Other search engines                            | Varies; assume the head only                                                                                                                               |

### Prerendering was considered and not done

Rendering `/` at build time is possible — Playwright is already a
dependency and already renders the social card. It was not done because this
application mounts with `createRoot().render()`, not `hydrateRoot()`: a
prerendered DOM would be **discarded and rebuilt** on mount, trading a
crawlable body for a flash and a layout shift.

Doing it properly means a hydration path, and hydration needs the server's
HTML to match the client's first render — which it cannot, because the
session is unknown at build time and `/` is two different pages depending on
it (`specs/product-experience.md` §40.10).

That is an architecture decision with an ADR's worth of consequences, not a
step at the end of an SEO task. **Recorded as an accepted limitation.**

## 7. Deployment requirements

Three things this bundle cannot do for itself:

1. **`VITE_PUBLIC_ORIGIN`** at build time, equal to the backend's
   `PUBLIC_APP_URL`. Without it the deployment is not indexable, by design.
2. **`X-Robots-Tag: noindex`** on the disallowed paths, if a bare URL
   appearing in results is unacceptable (§2).
3. **A real 404** for unknown paths, if soft 404s matter. Every path
   currently answers `200` with the SPA shell, because that is what history
   fallback is; the not-found page is rendered client-side. Google usually
   detects a soft 404, but "usually" is the accurate word.

## 8. Search Console

Ready: the canonical is stable, `sitemap.xml` and `robots.txt` are served,
and there is no indexing conflict between them — a test asserts the sitemap
never lists a path the policy disallows.

**No verification token is committed.** Adding one means adding the real
token the property issues; a placeholder would fail verification and a
guessed one is nonsense.

## 9. Scale

The sitemap lists one URL and needs no partitioning. If public content ever
becomes indexable — tournaments, or profiles under an opt-in — the threshold
to watch is **50,000 URLs or 50 MB**, at which point a sitemap index and
partitioned children are required rather than optional. Nothing here should
be built before that is a real number.

## 10. Localisation

The application ships Uzbek, Russian and English, and **the locale is not in
the URL** — it is a browser preference. So there is one canonical per page
and `hreflang` would be a claim about alternates that do not exist.

If locale-prefixed routes are ever introduced, `hreflang` and per-locale
canonicals become required at the same moment, and this section is where
that is written down.

## Related

- `specs/product-experience.md` §42 — the decisions and measurements
- `specs/product-experience.md` §40 — the landing page this exists to expose
- `docs/04-frontend/design-system.md` — brand and social asset rules
