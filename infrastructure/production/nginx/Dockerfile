# The production edge — A64-028.6A §4.
#
# ## Why the configuration is baked in rather than mounted
#
# A mounted config is a file on the host that nothing versions and nothing
# validates. Baking it makes the image and its behaviour one artefact: the
# tag that is deployed *is* the routing policy, a rollback rolls both back
# together, and `nginx -t` runs at **build** time, so a syntax error fails
# the build rather than a container start at 3am.
#
# The two things that genuinely vary at run time — the domain, and the
# certificate — arrive as an environment variable and a volume.
#
# ## Version pinning
#
# A minor tag rather than `latest` or a full pin. `latest` is a rollback
# nobody can describe; a full patch pin means a CVE in nginx waits for
# somebody to notice. `1.29-alpine` takes patch releases and rejects a minor
# upgrade, which is the level at which nginx changes directive semantics —
# `http2 on;` replaced `listen ... http2` in 1.25.
#
# Alpine because the edge should carry as little as possible that is not
# nginx. The image is verified below to include the modules this
# configuration needs, so a base change that dropped one fails the build.

FROM nginx:1.29-alpine

# --- What this configuration requires of the binary --------------------------
#
# Checked rather than assumed. `--with-http_v3_module` is the one that
# decides whether §9 implements HTTP/3 or defers it, and it is a property of
# the image rather than of the config — an image without it would start
# happily and silently serve no HTTP/3 at all.
RUN nginx -V 2>&1 | grep -q -- '--with-http_v3_module' \
	&& nginx -V 2>&1 | grep -q -- '--with-http_v2_module' \
	&& nginx -V 2>&1 | grep -q -- '--with-http_ssl_module' \
	|| (echo "This nginx build is missing a module the Arena64 edge requires." >&2 && exit 1)

COPY nginx.conf /etc/nginx/nginx.conf
COPY snippets/ /etc/nginx/snippets/
COPY conf.d/ /etc/nginx/conf.d/
COPY templates/ /etc/nginx/templates/

# --- The build-time gate — §4 -------------------------------------------------
#
# `nginx -t` needs the finished configuration, and the finished
# configuration needs the domain and a certificate. Both are supplied here as
# throwaways: a self-signed pair that never leaves the build stage, and a
# domain that is obviously not real.
#
# What this proves is **syntax and structure** — every `include` resolves,
# every directive is in a context that accepts it, every regex compiles. What
# it cannot prove is that the real certificate exists, which is the runtime
# check the entrypoint makes.
RUN apk add --no-cache --virtual .build-deps openssl gettext \
	&& mkdir -p /etc/letsencrypt/live/build.invalid /var/www/certbot \
	&& openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj '/CN=build.invalid' \
		-keyout /etc/letsencrypt/live/build.invalid/privkey.pem \
		-out /etc/letsencrypt/live/build.invalid/fullchain.pem 2>/dev/null \
	&& cp /etc/letsencrypt/live/build.invalid/fullchain.pem \
		/etc/letsencrypt/live/build.invalid/chain.pem \
	&& for template in /etc/nginx/templates/*.template; do \
		ARENA64_DOMAIN=build.invalid envsubst '$ARENA64_DOMAIN' \
			< "$template" > "/etc/nginx/conf.d/$(basename "$template" .template)"; \
	done \
	&& nginx -t \
	&& rm -f /etc/nginx/conf.d/10-arena64.conf /etc/nginx/conf.d/20-admin.conf \
	&& rm -rf /etc/letsencrypt/live/build.invalid \
	&& apk del .build-deps

# Only `$ARENA64_DOMAIN` is substituted. Without this filter `envsubst`
# would replace `$host`, `$uri`, `$request_id` and every other nginx
# variable with an empty string — the configuration would still parse and
# would proxy to nothing, log nothing useful, and redirect to `https:///`.
ENV NGINX_ENVSUBST_FILTER="ARENA64_DOMAIN"

# --- Root, and why --------------------------------------------------------
#
# The master process stays root and drops to `nginx` for the workers, which
# is the stock image's model and is what lets it bind 80 and 443. Running
# the master unprivileged means either a capability grant or a high port and
# a host-level redirect, and both trade a well-understood privilege
# separation for a less-well-understood one.
#
# What is worth saying is what root here can and cannot reach: no database
# credential, no application secret, and a certificate volume mounted for
# reading. `docs/01-architecture/deployment.md` §8.4 records the decision.
#
# The `nginx` user does need the certificate readable — see the compose
# file's note on why the private key's mode is what it is.

EXPOSE 80 443 443/udp

# The stock entrypoint runs the template substitution and starts nginx in
# the foreground, which is what makes `SIGTERM` reach the master and
# graceful shutdown work. Restating it would only be a chance to get it
# wrong.
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
	CMD wget --quiet --tries=1 --spider --no-check-certificate \
		https://localhost/health || exit 1
