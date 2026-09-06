#!/bin/sh
# Certificate lifecycle — A64-028.6A §6, §32; state machine split by A64-030.2.
#
# ## Why this exists at all
#
# Caddy obtained and renewed certificates itself; nginx does not. That is the
# single largest thing the nginx migration had to replace, and it is the one
# with a failure mode nobody notices until the site is unreachable: a
# certificate that quietly stops renewing works perfectly for eighty-nine
# days.
#
# ## Two modes, told apart by evidence
#
#   FIRST ISSUANCE   no Certbot lineage exists. Retry `issue.sh` on a short
#                    interval; every failure leaves the stopgap in place and
#                    the site untrusted, so the interval is minutes.
#
#   NORMAL RENEWAL   a Certbot lineage exists. `certbot renew` is a no-op
#                    until thirty days from expiry, so twice a day costs two
#                    processes and a log line and means a renewal happens on
#                    the first of sixty opportunities rather than the last.
#
# **The test that separates them is `has_certbot_lineage`** — a symlink under
# `live/<domain>` resolving into `archive/<domain>`, which is certbot's own
# layout and which nothing this repository writes can imitate.
#
# It used to be the presence of `.self-signed`, a marker Arena64 wrote into
# certbot's own directory. That is what A64-030.2 removed: the loop's notion
# of "have we succeeded yet" was a file we controlled, sitting in a directory
# whose contents were simultaneously making success impossible.
#
# ## Nginx is reloaded by nginx, not from here
#
# The obvious `--deploy-hook "nginx -s reload"` cannot work: nginx is a
# different container and this one has no signal path to it. Giving this
# container the Docker socket to run `docker exec` would hand a
# certificate-renewal job the ability to start any container on the host,
# which is a far larger grant than the problem needs.
#
# So nginx watches the stable symlink itself and reloads when what it
# resolves to changes — see its command in `compose.yml`. A new certificate
# is live within a minute of being written, and this container keeps no
# privilege it does not need.

set -eu

. /usr/local/bin/lineage.sh

: "${ARENA64_DOMAIN:?ARENA64_DOMAIN is required}"

#: Minutes, not hours, while the site is untrusted: every retry is a chance
#: to stop serving a browser warning.
RETRY_SECONDS=300

echo "certbot: certificate loop started"

while :; do
	if ! has_certbot_lineage "${ARENA64_DOMAIN}"; then
		# --- FIRST ISSUANCE ------------------------------------------------
		#
		# Nginx is up by now — that is what `issue.sh` exiting zero bought —
		# so there is finally something to answer the HTTP-01 challenge.
		echo "certbot: no certificate lineage yet; retrying issuance"
		# Through `sh`, exactly as `certbot-init`'s entrypoint invokes it: the
		# script is bind-mounted read-only and carries whatever mode the host
		# gave it, so nothing here may depend on the execute bit.
		sh /usr/local/bin/issue.sh || echo "certbot: issuance retry failed"
		sleep "${RETRY_SECONDS}"
		continue
	fi

	# --- NORMAL RENEWAL -----------------------------------------------------
	#
	# `|| echo`: a failed renewal must not stop the loop. The **existing**
	# certificate stays valid and in place — certbot writes atomically and
	# never removes a working certificate on failure — so a transient failure
	# costs nothing and a persistent one is caught by the expiry metric long
	# before the certificate lapses.
	certbot renew \
		--webroot --webroot-path /var/www/certbot \
		--non-interactive \
		--quiet \
		|| echo "certbot: renewal attempt failed; the existing certificate is unchanged"

	# Re-assert the stable path after every renewal pass. A renewal replaces
	# `live/<domain>`'s symlinks in place, so this is normally a no-op — but
	# it is what makes the loop self-healing if the link is ever lost, and it
	# costs one `readlink`.
	arena64_point_current_at "${ARENA64_DOMAIN}" "$(certbot_live_dir "${ARENA64_DOMAIN}")"

	# 12 hours, plus up to an hour of jitter.
	sleep $((43200 + RANDOM % 3600))
done
