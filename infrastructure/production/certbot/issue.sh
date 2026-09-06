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

echo "certbot: requesting a certificate for ${ARENA64_DOMAIN} and admin.${ARENA64_DOMAIN}"

# `--keep-until-expiring` so a re-run inside the validity window is free.
# Both names on one certificate: the admin console is a subdomain of the
# product and a single certificate is one thing to renew rather than two.
certbot certonly \
	--webroot --webroot-path /var/www/certbot \
	--non-interactive --agree-tos \
	--email "${ARENA64_ACME_EMAIL}" \
	--keep-until-expiring \
	--cert-name "${ARENA64_DOMAIN}" \
	-d "${ARENA64_DOMAIN}" -d "admin.${ARENA64_DOMAIN}"

rm -f "${LIVE}/.self-signed"
echo "certbot: issuance complete"
