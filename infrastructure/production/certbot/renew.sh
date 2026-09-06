#!/bin/sh
# Certificate lifecycle — A64-028.6A §6, §32.
#
# ## Why this exists at all
#
# Caddy obtained and renewed certificates itself; nginx does not. That is
# the single largest thing this migration had to replace, and it is the one
# with a failure mode nobody notices until the site is unreachable: a
# certificate that quietly stops renewing works perfectly for eighty-nine
# days.
#
# ## The loop
#
# `certbot renew` is a no-op until a certificate is within thirty days of
# expiry, so running it twice a day costs two processes and a log line and
# means a renewal happens on the first of sixty opportunities rather than
# the last. The randomised sleep spreads load on Let's Encrypt's servers,
# which their own guidance asks for.
#
# ## Issuance is attempted first, then finished here
#
# The first certificate is requested by `certbot-init` (see the compose
# file), which runs once and exits. That attempt can fail for reasons
# renewal cannot — DNS not yet pointed at this host, port 80 not open — and
# on a first boot it fails for a reason of its own: the HTTP-01 challenge is
# served by nginx, and nginx has not started yet, because it is waiting for
# that very container to finish.
#
# So `issue.sh` now writes its self-signed stopgap, reports the failure, and
# exits zero. Nginx starts on the stopgap and can answer a challenge, and
# this loop finishes what `certbot-init` began.
#
# Retrying does not hide the failure, which was the original objection to
# putting issuance in a loop. `.self-signed` stays on disk until a real
# certificate replaces it, the certificate being served expires in three
# days, and every retry logs. What would hide it is a container that exited
# non-zero on a host where nothing reads exit codes — A64-029.
#
# ## Nginx is reloaded by nginx, not from here
#
# The obvious `--deploy-hook "nginx -s reload"` cannot work: nginx is a
# different container and this one has no signal path to it. Giving this
# container the Docker socket to run `docker exec` would hand a
# certificate-renewal job the ability to start any container on the host,
# which is a far larger grant than the problem needs.
#
# So nginx reloads itself on a timer (see its compose command). A renewal
# is therefore live within six hours of being written, and the certificate
# is valid for thirty days at that point — the delay is irrelevant and the
# privilege saved is not.

set -eu

: "${ARENA64_DOMAIN:?ARENA64_DOMAIN is required}"

#: Written by `issue.sh` beside the stopgap, and removed the moment a real
#: certificate replaces it. Its presence is the one durable fact that says
#: "this host is serving a certificate nobody trusts".
STOPGAP="/etc/letsencrypt/live/${ARENA64_DOMAIN}/.self-signed"

#: Minutes, not hours, while the site is untrusted: every retry is a chance
#: to stop serving a browser warning. Once a real certificate is in place
#: the loop drops to the renewal cadence, where twelve hours means a renewal
#: happens on the first of sixty opportunities rather than the last.
RETRY_SECONDS=300

echo "certbot: certificate loop started"

while :; do
	if [ -f "${STOPGAP}" ]; then
		# Nginx is up by now — that is what `issue.sh` exiting zero bought —
		# so there is finally something to answer the HTTP-01 challenge.
		echo "certbot: still on the self-signed stopgap; retrying issuance"
		# Through `sh`, exactly as `certbot-init`'s entrypoint invokes it: the
		# script is bind-mounted read-only and carries whatever mode the host
		# gave it, so nothing here may depend on the execute bit.
		sh /usr/local/bin/issue.sh || echo "certbot: issuance retry failed"
		sleep "${RETRY_SECONDS}"
		continue
	fi

	# `|| echo`: a failed renewal must not stop the loop. The **existing**
	# certificate stays valid and in place — certbot writes atomically and
	# never removes a working certificate on failure — so a transient
	# failure costs nothing and a persistent one is caught by the expiry
	# metric long before the certificate lapses.
	certbot renew \
		--webroot --webroot-path /var/www/certbot \
		--non-interactive \
		--quiet \
		|| echo "certbot: renewal attempt failed; the existing certificate is unchanged"

	# 12 hours, plus up to an hour of jitter.
	sleep $((43200 + RANDOM % 3600))
done
