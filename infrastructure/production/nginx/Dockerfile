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

# --- The base image's own server block, which used to survive — A64-030.2 (E-1)
#
# `nginx:alpine` ships `/etc/nginx/conf.d/default.conf`: `listen 80;
# server_name localhost;` serving `/usr/share/nginx/html`. `COPY conf.d/`
# adds this repository's files beside it and removes nothing, and
# `nginx.conf` includes `conf.d/*.conf` — so the stock block was loaded in
# production alongside `00-redirect.conf`.
#
# Measured on the built image before this line existed:
#
#     GET / with Host: localhost  ->  200 "Welcome to nginx!"
#
# while every other Host correctly got a 301 to HTTPS. One request header
# was enough to opt out of the edge's HTTP→HTTPS policy and be served a page
# this deployment never intended to publish. Nothing was exposed beyond the
# stock page, and HTTPS was unaffected — `default.conf` listens only on 80 —
# but a policy with a bypass is not the policy.
#
# ## Why an assertion and not just `rm`
#
# `rm` fixes today's file. The defect was never that *this* file existed; it
# was that nothing noticed the base image had put something in a directory
# this configuration includes wholesale. A future `nginx:alpine` adding a
# second default would reintroduce it silently, and the deletion of one
# known name would still look correct.
#
# So the directory is asserted to contain **exactly** what this repository
# put there. A base image that ships anything new fails the build, which is
# where a surprise like this should surface — not in a response header on a
# production host. `tests/unit/test_edge_policy.py` guards that this
# assertion is still here.
RUN rm -f /etc/nginx/conf.d/default.conf \
	&& expected="$(cd /etc/nginx/conf.d && ls -A | sort | tr '\n' ' ')" \
	&& if [ "$expected" != "00-redirect.conf " ]; then \
		echo "conf.d holds files this repository did not put there: ${expected}" >&2; \
		echo "The base image ships its own server blocks and nginx.conf includes" >&2; \
		echo "conf.d/*.conf wholesale. Review them, then update this assertion." >&2; \
		exit 1; \
	fi

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
	# Rendered copies exist only for the test above. They are removed by the
	# same rule that put them there, so a template added later cannot leave a
	# stale build-time render behind — the assertion above would fail the
	# next build if one did.
	&& for template in /etc/nginx/templates/*.template; do \
		rm -f "/etc/nginx/conf.d/$(basename "$template" .template)"; \
	done \
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
# `/healthz`, not `/health` — A64-030.2 (NB-2).
#
# `/health` is proxied to the API, so this reported the *application's*
# health under the edge's name. On a first boot nginx starts before any API
# replica exists, by design, so the edge spent that whole window marked
# `unhealthy` while serving the ACME challenge correctly. `/healthz` is
# answered by nginx itself and asks only "did this process start and can it
# terminate TLS", which is the one question a container healthcheck on the
# edge can answer.
#
# `--no-check-certificate` because the certificate names the domain and this
# connects to `localhost`; the probe is inside the container and is not
# authenticating anything.
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
	CMD wget --quiet --tries=1 --spider --no-check-certificate \
		https://localhost/healthz || exit 1
