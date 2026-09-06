#!/bin/sh
# First issuance — A64-028.6A §6.
#
# Runs once and exits. Idempotent: an existing certificate for this domain
# short-circuits, so re-running a deploy does not spend a rate-limit slot.
#
# ## Why a self-signed certificate is written first
#
# Nginx will not start without the files its `ssl_certificate` directives
# name, and certbot's HTTP-01 challenge is served **by nginx** on port 80.
# That is a deadlock: no certificate, no nginx; no nginx, no challenge.
#
# The stopgap is a self-signed pair at the real path. Nginx starts, serves
# the challenge over plain HTTP, certbot replaces the files with the real
# certificate, and nginx's reload loop picks them up. A browser reaching the
# site in that window sees a certificate warning, which is the correct
# signal for a deployment that is not finished.
#
# The alternative — a separate HTTP-only nginx for bootstrap — is a second
# configuration for the same edge, which is a second thing to keep in step.

set -eu

: "${ARENA64_DOMAIN:?ARENA64_DOMAIN is required}"
: "${ARENA64_ACME_EMAIL:?ARENA64_ACME_EMAIL is required}"

LIVE="/etc/letsencrypt/live/${ARENA64_DOMAIN}"

if [ -f "${LIVE}/fullchain.pem" ] && [ ! -f "${LIVE}/.self-signed" ]; then
	echo "certbot: a certificate for ${ARENA64_DOMAIN} already exists — nothing to do"
	exit 0
fi

if [ ! -f "${LIVE}/fullchain.pem" ]; then
	echo "certbot: writing a self-signed stopgap so nginx can start and serve the challenge"
	mkdir -p "${LIVE}"
	openssl req -x509 -newkey rsa:2048 -nodes -days 3 \
		-subj "/CN=${ARENA64_DOMAIN}" \
		-keyout "${LIVE}/privkey.pem" \
		-out "${LIVE}/fullchain.pem" 2>/dev/null
	cp "${LIVE}/fullchain.pem" "${LIVE}/chain.pem"
	touch "${LIVE}/.self-signed"
	chmod 640 "${LIVE}/privkey.pem"
fi

echo "certbot: requesting a certificate for ${ARENA64_DOMAIN}, www.${ARENA64_DOMAIN} and admin.${ARENA64_DOMAIN}"

# `--keep-until-expiring` so a re-run inside the validity window is free.
# All three names on one certificate: the admin console and `www` are
# subdomains of the product and a single certificate is one thing to renew
# rather than three.
#
# **`www` was added by A64-030.2 and it couples issuance to that DNS record.**
# ACME validates every name in an order, and one name that cannot be
# validated fails the whole order — so if `www.${ARENA64_DOMAIN}` ever stops
# resolving to this host, the apex and `admin.` stop renewing with it. That
# is the same coupling `admin.` has always carried, now with one more name
# in it. Removing `www` is therefore three edits in one change: the DNS
# record, this line, and `nginx/templates/30-www.conf.template`.
# `deployment.md` §8.10 states the alternative and why it was not chosen.
#
# **The failure is survived rather than fatal, and that is A64-029's
# deployment blocker.** The challenge above is served by nginx, and nginx
# waits on this container with `condition: service_completed_successfully`.
# Exiting non-zero meant nginx never started, so nothing ever answered the
# challenge, so this could never succeed — on every first boot, for ever.
# Measured on a clean host: eleven of fifteen services stayed in `created`
# and nothing listened on 80 or 443.
#
# That is exactly the deadlock the stopgap above was written to break, and
# the exit code put it straight back.
if certbot certonly \
	--webroot --webroot-path /var/www/certbot \
	--non-interactive --agree-tos \
	--email "${ARENA64_ACME_EMAIL}" \
	--keep-until-expiring \
	--cert-name "${ARENA64_DOMAIN}" \
	-d "${ARENA64_DOMAIN}" -d "www.${ARENA64_DOMAIN}" -d "admin.${ARENA64_DOMAIN}"; then
	rm -f "${LIVE}/.self-signed"
	echo "certbot: issuance complete"
	exit 0
fi

# Nothing is hidden by exiting zero. `.self-signed` stays on disk — it is
# what `renew.sh` retries on and what an operator greps for — and the
# certificate nginx then serves expires in three days, far inside any expiry
# alert. A deployment that is not finished says so with a browser warning,
# which §6 already chose as the correct signal.
echo "certbot: issuance FAILED — nginx will start on the self-signed stopgap" >&2
echo "certbot: the renewal loop retries; the site is untrusted until it succeeds" >&2
exit 0
