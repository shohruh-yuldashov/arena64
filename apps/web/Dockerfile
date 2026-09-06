# The player-facing client, built once and served as files — A64-028.6 §13.
#
# Closes the client half of A64-028.1's P0-2: "`compose.yml` defines `api`,
# `migrate`, `caddy`, `postgres`, `redis`, `minio` — no client. There is
# nothing to serve."
#
# ## Why the output is a scratch image and not a server
#
# This image runs nothing. It exists to carry `dist/` into the deployment,
# where the edge container serves it from a shared volume. A second web
# server behind Caddy would be a second place for cache headers, a second
# place for a security header to be forgotten, and a second process to
# operate — and §14 already makes the edge responsible for all of it.
#
# ## Why the origin is a build argument
#
# `VITE_PUBLIC_ORIGIN` is read by `scripts/generate-seo.mjs` at build time
# to write the canonical link, `og:url`, the absolute social image and the
# sitemap. It cannot be a runtime variable: those values are baked into
# `index.html`.
#
# It has **no default**, deliberately. The generator fails closed — without
# an origin it writes a `robots.txt` that blocks everything — and a default
# here would turn that fail-safe into a silent wrong answer.

FROM node:22-bookworm-slim AS build

WORKDIR /build

# The lockfile first, so a dependency install is cached independently of a
# source change. `npm ci` rather than `npm install`: it refuses to resolve
# anything the lockfile does not already pin.
COPY package.json package-lock.json ./
RUN npm ci

COPY . .

ARG VITE_PUBLIC_ORIGIN
ENV VITE_PUBLIC_ORIGIN=${VITE_PUBLIC_ORIGIN}

# `npm run build` is `tsc --noEmit && vite build && node scripts/generate-seo.mjs`.
# The type check is part of the build on purpose: an image that compiles but
# does not type-check is one whose next change is unreviewable.
# Two checks the generator cannot make for itself. `generate-seo.mjs`
# accepts an `http://localhost` origin deliberately — that is what a
# developer previewing a build wants — and a production image is exactly
# where that affordance becomes a defect: canonical, `og:url` and the
# sitemap would all name a machine nobody else has, and search would index
# it or refuse it.
RUN case "${VITE_PUBLIC_ORIGIN}" in \
	"") echo "VITE_PUBLIC_ORIGIN is required: without it the SEO step writes a robots.txt that blocks everything." >&2; exit 1 ;; \
	https://*) ;; \
	*) echo "VITE_PUBLIC_ORIGIN must be https:// for a production image, got '${VITE_PUBLIC_ORIGIN}'." >&2; exit 1 ;; \
	esac \
	&& case "${VITE_PUBLIC_ORIGIN}" in \
	*localhost*|*127.0.0.1*|*.local|*.local/*) echo "VITE_PUBLIC_ORIGIN names a development host: '${VITE_PUBLIC_ORIGIN}'. A production build would put it in the canonical link, og:url and the sitemap." >&2; exit 1 ;; \
	esac \
	&& npm run build

# Source maps are built (`vite.config.ts` sets `sourcemap: true`, and that
# file is the user's) and **not shipped**. A public source map hands an
# attacker the unminified application; keeping them out of the image is the
# deployment-layer answer that does not touch the config.
RUN find dist -name '*.map' -delete

FROM scratch AS dist
COPY --from=build /build/dist /dist
