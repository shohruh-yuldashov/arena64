#!/bin/sh
# First issuance — A64-028.6A §6, rewritten by A64-030.2 and A64-030.3.
#
# Runs once from `certbot-init` and again from `renew.sh`'s retry loop.
# Idempotent: an existing lineage short-circuits it, so re-running a deploy
# does not spend a rate-limit slot.
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
# ## Where the stopgap lives — A64-030.2
#
# It used to live in `/etc/letsencrypt/live/<domain>/`. That directory
# belongs to certbot, and `RenewableCert.new_lineage` refuses to create a
# lineage over it — so the first real production issuance validated all
# three names, received a certificate from Let's Encrypt, and then threw it
# away with `CertStorageError: live directory exists for arena64.gg`.
#
# So the stopgap lives under `/etc/letsencrypt/arena64/`, which certbot
# neither reads nor writes, and nginx reads a **stable symlink** —
# `arena64/current/<domain>` — that points at the stopgap before issuance
# and at Certbot's lineage after it.
#
# ## Which lineage, though — A64-030.3
#
# This script used to answer that with `live/$ARENA64_DOMAIN`. Certbot never
# promised that name: `unique_lineage_name` returns `<domain>-0001` when the
# base name is taken, and on the affected host that is exactly what it did.
# The certificate was real, trusted and correctly stored — and invisible to
# every script here.
#
# So both the "have we already got one?" question and the post-issuance
# "what did certbot just create?" question go through
# `arena64_discover_lineage`, which finds the lineage rather than assuming
# its name. `lineage.sh` carries the boundary, the evidence tests and the
# four states.

set -eu

. /usr/local/bin/lineage.sh

: "${ARENA64_DOMAIN:?ARENA64_DOMAIN is required}"
: "${ARENA64_ACME_EMAIL:?ARENA64_ACME_EMAIL is required}"

STOPGAP="$(arena64_stopgap_dir "${ARENA64_DOMAIN}")"

# Republish the public certificate for the expiry metric — A64-030.4B.1.
#
# Never fatal. The worker reading a stale expiry date is a smaller problem
# than an issuance path that aborts because an observability copy could not
# be written, and `arena64_project_public_certificate` has already said why
# on stderr.
publish_public_certificate() {
	arena64_project_public_certificate "${ARENA64_DOMAIN}" ||
		echo "certbot: the observability certificate was not refreshed" >&2
}

# `set -e` would kill the script on any status but zero, and three of the
# four are ordinary answers this script exists to handle.
LINEAGE="$(arena64_discover_lineage "${ARENA64_DOMAIN}")" && STATUS=0 || STATUS=$?

echo "certbot: lineage discovery reports $(arena64_lineage_status_name "${STATUS}")"

# --- Already done? --------------------------------------------------------
if [ "${STATUS}" -eq "${ARENA64_LINEAGE_FOUND}" ]; then
	arena64_point_current_at "${ARENA64_DOMAIN}" "${LINEAGE}"
	publish_public_certificate
	echo "certbot: ${LINEAGE} already holds a certificate for ${ARENA64_DOMAIN} — nothing to do"
	exit 0
fi

# --- The stopgap ----------------------------------------------------------
#
# Written into Arena64's own directory. `-days 3` so that a deployment stuck
# here fails visibly long before anybody could mistake it for finished.
write_stopgap() {
	if [ ! -f "${STOPGAP}/fullchain.pem" ]; then
		echo "certbot: writing a self-signed stopgap so nginx can start and serve the challenge"
		mkdir -p "${STOPGAP}"
		openssl req -x509 -newkey rsa:2048 -nodes -days 3 \
			-subj "/CN=${ARENA64_DOMAIN}" \
			-addext "subjectAltName=DNS:${ARENA64_DOMAIN},DNS:www.${ARENA64_DOMAIN},DNS:admin.${ARENA64_DOMAIN}" \
			-keyout "${STOPGAP}/privkey.pem" \
			-out "${STOPGAP}/fullchain.pem" 2>/dev/null
		cp "${STOPGAP}/fullchain.pem" "${STOPGAP}/chain.pem"
		chmod 640 "${STOPGAP}/privkey.pem"
		# Kept for an operator grepping for "is this host still untrusted?".
		# It is **not** what any decision is made on — see `lineage.sh`.
		: > "${STOPGAP}/.self-signed"
	fi
}

# --- Ambiguous or malformed: stop, but leave the edge able to start --------
#
# Discovery has already said why on stderr. What must not happen here is an
# ACME request: both production incidents ended with a spent rate-limit slot
# and a certificate nothing served, and both began with code that carried on
# past a state it did not understand.
#
# Exiting zero is still required — nginx waits on this container with
# `service_completed_successfully`, and a non-zero exit is the A64-029
# deadlock. So the edge comes up; it just comes up untrusted, loudly.
if [ "${STATUS}" -ne "${ARENA64_LINEAGE_NONE}" ]; then
	echo "certbot: NOT requesting a certificate while the state is $(arena64_lineage_status_name "${STATUS}")" >&2
	# Only touch the stable path if nginx would otherwise find nothing —
	# on a host that is already serving a real certificate through it, the
	# broken sibling lineage is a thing to fix, not a reason to downgrade.
	if ! arena64_current_is_usable "${ARENA64_DOMAIN}"; then
		write_stopgap
		arena64_point_current_at "${ARENA64_DOMAIN}" "${STOPGAP}"
		publish_public_certificate
		echo "certbot: the edge starts on the self-signed stopgap until an operator resolves this" >&2
	else
		echo "certbot: leaving the stable path at $(arena64_current_target "${ARENA64_DOMAIN}")" >&2
	fi
	exit 0
fi

# --- First issuance -------------------------------------------------------
write_stopgap

# Doing this on every run is what makes a restart mid-bootstrap converge
# rather than leaving nginx with a dangling link.
arena64_point_current_at "${ARENA64_DOMAIN}" "${STOPGAP}"
publish_public_certificate

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
# `--cert-name` is a *request*, not a guarantee. Certbot honours it only if
# the name is free; otherwise it appends `-0001` and says so in one line of
# output nobody reads. That is why success is confirmed by discovery below
# and not by this flag.
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
	# the first incident was certbot exiting non-zero *after* a successful
	# ACME order, and the mirror — a zero exit with no usable lineage —
	# would be just as bad to act on. Ask where the lineage is rather than
	# where it ought to be; the second incident was the difference.
	LINEAGE="$(arena64_discover_lineage "${ARENA64_DOMAIN}")" && STATUS=0 || STATUS=$?
	if [ "${STATUS}" -eq "${ARENA64_LINEAGE_FOUND}" ]; then
		arena64_point_current_at "${ARENA64_DOMAIN}" "${LINEAGE}"
		publish_public_certificate
		echo "certbot: issuance complete; ${ARENA64_DOMAIN} now serves the certificate at ${LINEAGE}"
		echo "certbot: nginx picks it up on its next reload check"
		exit 0
	fi

	echo "certbot: certbot reported success but discovery reports $(arena64_lineage_status_name "${STATUS}")" >&2
	echo "certbot: staying on the stopgap rather than serving a certificate nobody has validated" >&2
	exit 0
fi

# Nothing is hidden by exiting zero. The stopgap stays on disk, the
# certificate nginx serves expires in three days — far inside any expiry
# alert — and every retry logs. A deployment that is not finished says so
# with a browser warning, which §6 already chose as the correct signal.
echo "certbot: issuance FAILED — nginx will start on the self-signed stopgap" >&2
echo "certbot: the renewal loop retries; the site is untrusted until it succeeds" >&2
exit 0
