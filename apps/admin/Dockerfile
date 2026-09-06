# The operator console, built once and served as files — A64-028.6 §13.
#
# The same shape as `apps/web/Dockerfile` and for the same reasons; see that
# file for why the output is a `scratch` image rather than a server.
#
# Two differences, both deliberate:
#
#   - no `VITE_PUBLIC_ORIGIN`. The console is not indexed, has no canonical
#     URL, no social card and no sitemap — `index.html` already carries
#     `<meta name="robots" content="noindex, nofollow">` and the edge sets
#     the header. There is nothing for an origin to fill in.
#
#   - source maps were never generated (`apps/admin/vite.config.ts` has no
#     `build` block), so there is nothing to delete.
#
# **The console's security boundary is the API's authorization, not this
# image.** A bundle is public the moment it is served; every privileged
# action it can invoke is checked server-side, and hiding a frontend route
# has never been a control.

# The Node version is **not** chosen here. `.nvmrc` names it once, CI reads it
# through `node-version-file`, and this line must agree with it.
#
# It did not, and that is the whole of A64-029's blocker. `.nvmrc` pins
# 24.13.0; this said `node:22-bookworm-slim`. Node 22 ships npm 10.9.8, which
# resolves `@tailwindcss/oxide-wasm32-wasi`'s **bundled** dependencies at
# different patches than the npm 11 that wrote the lock — so `npm ci` refused
# a lock file that is correct everywhere else:
#
#     npm error `npm ci` can only install packages when your package.json and
#     package-lock.json ... are in sync.
#     npm error Missing: @emnapi/core@1.11.3 from lock file
#
# `f7df1f1` diagnosed exactly this and fixed it for CI. The image build was
# the one place the fix did not reach, so the release artifact was the one
# thing that could not be built. `tests/unit/test_build_toolchain.py` now
# fails if this line and `.nvmrc` disagree again.
FROM node:24.13.0-bookworm-slim AS build

WORKDIR /build

COPY package.json package-lock.json ./
RUN npm ci

COPY . .

RUN npm run build

FROM scratch AS dist
COPY --from=build /build/dist /dist
