#!/bin/sh
# Certificate lifecycle — A64-028.6A §6, §32; state machine split by
# A64-030.2 and taught to find the lineage by A64-030.3.
#
# ## Why this exists at all
#
# Caddy obtained and renewed certificates itself; nginx does not. That is the
# single largest thing the nginx migration had to replace, and it is the one
# with a failure mode nobody notices until the site is unreachable: a
# certificate that quietly stops renewing works perfectly for eighty-nine
# days.
#
# ## Three modes, told apart by evidence
#
#   FIRST ISSUANCE   discovery finds no lineage. Retry `issue.sh` on a short
#                    interval; every failure leaves the stopgap in place and
#                    the site untrusted, so the interval is minutes.
#
#   NORMAL RENEWAL   discovery finds exactly one. `certbot renew` is a no-op
#                    until thirty days from expiry, so twice a day costs two
#                    processes and a log line and means a renewal happens on
#                    the first of sixty opportunities rather than the last.
#
#   HELD             discovery finds ambiguity or wreckage. Log it and do
#                    nothing else — no renewal, and above all no issuance.
#
# **The test that separates them is `arena64_discover_lineage`**, which
# validates Certbot's own storage layout rather than trusting a name.
#
# It used to be the presence of `.self-signed`, a marker Arena64 wrote into
# certbot's own directory — A64-030.2 removed that. Then it was
# `has_certbot_lineage "$ARENA64_DOMAIN"`, which A64-030.3 removed for a
# subtler reason: on the affected host the real lineage was called
# `arena64.gg-0001`, so that test answered "no lineage" against a valid
# ninety-day certificate. This loop would have called `issue.sh` every five
# minutes and bought a duplicate certificate on each pass until the weekly
# rate limit stopped it.
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

#: Held state needs a human, and humans are slower than five minutes. Long
#: enough that the log is readable, short enough that a fix is picked up
#: within the hour without anybody restarting this container.
HOLD_SECONDS=3600

echo "certbot: certificate loop started"

while :; do
	LINEAGE="$(arena64_discover_lineage "${ARENA64_DOMAIN}")" && STATUS=0 || STATUS=$?

	if [ "${STATUS}" -eq "${ARENA64_LINEAGE_NONE}" ]; then
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

	if [ "${STATUS}" -ne "${ARENA64_LINEAGE_FOUND}" ]; then
		# --- HELD ----------------------------------------------------------
		#
		# Discovery has already explained itself on stderr. The one thing
		# this branch must never do is fall through to issuance: buying a
		# certificate is how a host with a confusing certificate state
		# acquires a second confusing certificate.
		echo "certbot: HELD — state is $(arena64_lineage_status_name "${STATUS}"); no renewal, no issuance" >&2
		echo "certbot: the edge keeps serving $(arena64_current_target "${ARENA64_DOMAIN}")" >&2
		sleep "${HOLD_SECONDS}"
		continue
	fi

	# --- NORMAL RENEWAL -----------------------------------------------------
	#
	# `|| echo`: a failed renewal must not stop the loop. The **existing**
	# certificate stays valid and in place — certbot writes atomically and
	# never removes a working certificate on failure — so a transient failure
	# costs nothing and a persistent one is caught by the expiry metric long
	# before the certificate lapses.
	echo "certbot: renewing from ${LINEAGE} if it is due"
	certbot renew \
		--webroot --webroot-path /var/www/certbot \
		--non-interactive \
		--quiet \
		|| echo "certbot: renewal attempt failed; the existing certificate is unchanged"

	# Re-assert the stable path after every renewal pass, through discovery
	# rather than through the name we started with. A renewal replaces
	# `live/<name>`'s symlinks in place, so this is normally a no-op — but it
	# is what makes the loop self-healing if the link is ever lost, and it
	# costs one pass over a directory.
	#
	# If discovery no longer agrees, say so and change nothing: whatever the
	# edge is serving now is a certificate that worked a moment ago, and
	# repointing it on the strength of a state we just called broken is the
	# opposite of safe.
	LINEAGE="$(arena64_discover_lineage "${ARENA64_DOMAIN}")" && STATUS=0 || STATUS=$?
	if [ "${STATUS}" -eq "${ARENA64_LINEAGE_FOUND}" ]; then
		arena64_point_current_at "${ARENA64_DOMAIN}" "${LINEAGE}"
		# The public copy the expiry metric reads — A64-030.4B.1. A renewal
		# replaces `fullchainN.pem` with `fullchainN+1.pem`, so without this
		# the metric would keep reporting the date of the certificate the
		# renewal just replaced.
		arena64_project_public_certificate "${ARENA64_DOMAIN}" ||
			echo "certbot: the observability certificate was not refreshed" >&2
	else
		echo "certbot: post-renewal discovery reports $(arena64_lineage_status_name "${STATUS}"); leaving the stable path alone" >&2
	fi

	# 12 hours, plus up to an hour of jitter.
	sleep $((43200 + RANDOM % 3600))
done
