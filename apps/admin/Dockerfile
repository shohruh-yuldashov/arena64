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

FROM node:22-bookworm-slim AS build

WORKDIR /build

COPY package.json package-lock.json ./
RUN npm ci

COPY . .

RUN npm run build

FROM scratch AS dist
COPY --from=build /build/dist /dist
