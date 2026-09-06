#!/bin/sh
# First issuance — A64-028.6A §6, rewritten by A64-030.2.
#
# Runs once from `certbot-init` and again from `renew.sh`'s retry loop.
# Idempotent: a real lineage short-circuits it, so re-running a deploy does
# not spend a rate-limit slot.
#
# ## Why a stopgap certificate is written first
#
# Nginx will not start without the files its `ssl_certificate` directives
# name, and certbot's HTTP-01 challenge is served **by nginx** on port 80.
# That is a deadlock: no certificate, no nginx; no nginx, no challenge.
#
# The stopgap breaks it. Nginx starts on a self-signed pair, serves the
# challenge over plain HTTP, certbot obtains the real certificate, and the
# stable path this script maintains is repointed at it. A browser reaching
# the site in that window sees a certificate warning, which is the correct
# signal for a deployment that is not finished.
#
# ## Where the stopgap lives, and why that is the whole of A64-030.2
#
# It used to live in `/etc/letsencrypt/live/<domain>/`. That directory
# belongs to certbot, and `RenewableCert.new_lineage` refuses to create a
# lineage over it — so the first real production issuance validated all
# three names, received a certificate from Let's Encrypt, and then threw it
# away with `CertStorageError: live directory exists for arena64.gg`.
# Permanently, on every retry, because the stopgap is never removed while
# issuance keeps failing.
#
# So the stopgap now lives under `/etc/letsencrypt/arena64/`, which certbot
# neither reads nor writes, and nginx reads a **stable symlink** —
# `arena64/current/<domain>` — that points at the stopgap before issuance
# and at `live/<domain>` after it. Nginx's configuration never changes;
# only what one symlink resolves to does.
#
# `lineage.sh` carries the boundary and the evidence test.

set -eu

. /usr/local/bin/lineage.sh

: "${ARENA64_DOMAIN:?ARENA64_DOMAIN is required}"
: "${ARENA64_ACME_EMAIL:?ARENA64_ACME_EMAIL is required}"

STOPGAP="$(arena64_stopgap_dir "${ARENA64_DOMAIN}")"
LIVE="$(certbot_live_dir "${ARENA64_DOMAIN}")"

# --- Already done? --------------------------------------------------------
#
# Tested against certbot's own layout rather than a marker this script
# wrote: `live/<domain>/fullchain.pem` must be a symlink resolving into
# `archive/<domain>/`. Nothing Arena64 creates can satisfy that.
if has_certbot_lineage "${ARENA64_DOMAIN}"; then
	arena64_point_current_at "${ARENA64_DOMAIN}" "${LIVE}"
	echo "certbot: a real certificate for ${ARENA64_DOMAIN} already exists — nothing to do"
	exit 0
fi

# --- The stopgap ----------------------------------------------------------
#
# Written into Arena64's own directory. `-days 3` so that a deployment stuck
# here fails visibly long before anybody could mistake it for finished.
if [ ! -f "${STOPGAP}/fullchain.pem" ]; then
	echo "certbot: writing a self-signed stopgap so nginx can start and serve the challenge"
	mkdir -p "${STOPGAP}"
	openssl req -x509 -newkey rsa:2048 -nodes -days 3 \
		-subj "/CN=${ARENA64_DOMAIN}" \
		-keyout "${STOPGAP}/privkey.pem" \
		-out "${STOPGAP}/fullchain.pem" 2>/dev/null
	cp "${STOPGAP}/fullchain.pem" "${STOPGAP}/chain.pem"
	chmod 640 "${STOPGAP}/privkey.pem"
	# Kept for an operator grepping for "is this host still untrusted?".
	# It is **not** what any decision is made on — see `lineage.sh`.
	: > "${STOPGAP}/.self-signed"
fi

# Point the stable path at the stopgap unless it already resolves somewhere
# valid. Doing this on every run is what makes a restart mid-bootstrap
# converge rather than leaving nginx with a dangling link.
if ! has_certbot_lineage "${ARENA64_DOMAIN}"; then
	arena64_point_current_at "${ARENA64_DOMAIN}" "${STOPGAP}"
fi

echo "certbot: requesting a certificate for ${ARENA64_DOMAIN}, www.${ARENA64_DOMAIN} and admin.${ARENA64_DOMAIN}"

# `--keep-until-expiring` so a re-run inside the validity window is free.
# All three names on one certificate: the admin console and `www` are
# subdomains of the product and a single certificate is one thing to renew
# rather than three.
#
# **`www` couples issuance to that DNS record.** ACME validates every name in
# an order, and one name that cannot be validated fails the whole order — so
# if `www.${ARENA64_DOMAIN}` stops resolving to this host, the apex and
# `admin.` stop renewing with it. Removing `www` is three edits in one
# change: the DNS record, this line, and
# `nginx/templates/30-www.conf.template`. `deployment.md` §8.10 states the
# alternative and why it was not chosen.
#
# **The failure is survived rather than fatal, and that is A64-029's
# deployment blocker.** The challenge above is served by nginx, and nginx
# waits on this container with `condition: service_completed_successfully`.
# Exiting non-zero meant nginx never started, so nothing ever answered the
# challenge, so this could never succeed — on every first boot, for ever.
if certbot certonly \
	--webroot --webroot-path /var/www/certbot \
	--non-interactive --agree-tos \
	--email "${ARENA64_ACME_EMAIL}" \
	--keep-until-expiring \
	--cert-name "${ARENA64_DOMAIN}" \
	-d "${ARENA64_DOMAIN}" -d "www.${ARENA64_DOMAIN}" -d "admin.${ARENA64_DOMAIN}"; then

	# Certbot reported success. Believe the filesystem, not the exit code:
	# the incident this rewrite closes was certbot exiting non-zero *after*
	# a successful ACME order, and the mirror — a zero exit with no usable
	# lineage — would be just as bad to act on.
	if has_certbot_lineage "${ARENA64_DOMAIN}"; then
		arena64_point_current_at "${ARENA64_DOMAIN}" "${LIVE}"
		echo "certbot: issuance complete; ${ARENA64_DOMAIN} now serves a real certificate"
		echo "certbot: nginx picks it up on its next reload check"
		exit 0
	fi

	echo "certbot: certbot reported success but no lineage appeared at ${LIVE}" >&2
	echo "certbot: staying on the stopgap; this is the state recover-legacy-stopgap.sh exists for" >&2
	exit 0
fi

# Nothing is hidden by exiting zero. The stopgap stays on disk, the
# certificate nginx serves expires in three days — far inside any expiry
# alert — and every retry logs. A deployment that is not finished says so
# with a browser warning, which §6 already chose as the correct signal.
echo "certbot: issuance FAILED — nginx will start on the self-signed stopgap" >&2
echo "certbot: the renewal loop retries; the site is untrusted until it succeeds" >&2
exit 0
