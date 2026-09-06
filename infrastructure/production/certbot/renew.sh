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
# ## Issuance is separate, and deliberately manual-once
#
# The first certificate is obtained by `certbot-init` (see the compose
# file), which runs once and exits. Issuance can fail for reasons renewal
# cannot — DNS not yet pointed at this host, port 80 not open — and a loop
# that retried it for ever would hide that behind a container that looks
# like it is working.
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

echo "certbot: renewal loop started, checking every 12 hours"

while :; do
	# `|| true`: a failed renewal must not stop the loop. The **existing**
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
