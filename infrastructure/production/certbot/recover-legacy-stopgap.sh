#!/bin/sh
# One-time recovery for a host that ran the pre-A64-030.2 bootstrap.
#
#   docker compose --env-file production.env run --rm \
#     --entrypoint sh certbot /usr/local/bin/recover-legacy-stopgap.sh
#
# Quarantines — never deletes — the two pieces of state the old bootstrap
# left in Certbot's namespace, so that Certbot can create a lineage under
# the name the edge actually points at.
#
# ## What it moves, and why each one has to go
#
# **`live/<domain>/`** holding regular `.pem` files. `new_lineage` refuses
# outright: `CertStorageError: live directory exists for <domain>`. This is
# the error the first production issuance died on, after Let's Encrypt had
# already issued the certificate.
#
# **`renewal/<domain>.conf`** left behind, empty, by that failed attempt.
# `util.unique_lineage_name` creates the file with `safe_open` *before* the
# live-directory guard runs, and the guard's `raise` does not unlink it. Its
# presence is not harmless: on the next attempt `unique_lineage_name` finds
# the name taken and returns `<domain>-0001.conf`, so certbot creates a
# lineage called **`<domain>-0001`** at `live/<domain>-0001/`. That succeeds
# — quietly — and produces a certificate the edge does not read.
#
# Both behaviours were verified against certbot v5.8.0's own storage module
# offline, and `tests/unit/test_acme_bootstrap.py` reproduces them.
#
# ## What it refuses
#
# Anything that looks like a real certificate. A Certbot lineage is symlinks
# under `live/` resolving into `archive/`; if that is what is there, this
# script exits non-zero and changes nothing. It also refuses when the legacy
# fingerprint is absent, or when an archive lineage exists — either means
# the state is not what this script was written for, and guessing with
# somebody's private key is not a thing to do.
#
# Idempotent: a second run finds nothing to quarantine and exits 0.
#
# Nothing here prints key material, and nothing is removed — the quarantine
# directory keeps it all for inspection.

set -eu

. /usr/local/bin/lineage.sh

: "${ARENA64_DOMAIN:?ARENA64_DOMAIN is required}"

LIVE="$(certbot_live_dir "${ARENA64_DOMAIN}")"
ARCHIVE="/etc/letsencrypt/archive/${ARENA64_DOMAIN}"
RENEWAL="/etc/letsencrypt/renewal/${ARENA64_DOMAIN}.conf"
QUARANTINE="${ARENA64_STATE}/quarantine/$(date -u +%Y%m%dT%H%M%SZ)"

echo "recover: inspecting ${ARENA64_DOMAIN}"

# --- Refusal 1: a real certificate is present -------------------------------
if has_certbot_lineage "${ARENA64_DOMAIN}"; then
	echo "recover: REFUSING — ${LIVE} is a real Certbot lineage." >&2
	echo "recover: This host has a working certificate. Nothing to recover." >&2
	exit 1
fi

legacy_live=0
if [ -d "${LIVE}" ]; then
	# --- Refusal 2: present but not the shape this script understands -------
	#
	# The legacy fingerprint is regular files where Certbot would have put
	# symlinks. `.self-signed` is the strong marker the old bootstrap wrote;
	# it is required, so that a directory of unknown provenance is never
	# moved by this script.
	if [ ! -f "${LIVE}/.self-signed" ]; then
		echo "recover: REFUSING — ${LIVE} exists but carries no .self-signed marker." >&2
		echo "recover: This is not the legacy Arena64 stopgap. Inspect it by hand." >&2
		exit 1
	fi
	if [ -L "${LIVE}/fullchain.pem" ]; then
		echo "recover: REFUSING — ${LIVE}/fullchain.pem is a symlink." >&2
		echo "recover: Certbot uses symlinks; the legacy stopgap used regular files." >&2
		exit 1
	fi
	legacy_live=1
fi

# --- Refusal 3: an archive lineage exists -----------------------------------
#
# `archive/<domain>` holding anything means Certbot has written real
# certificate generations for this name. Moving `live/` out from under that
# would orphan them.
if [ -d "${ARCHIVE}" ] && [ -n "$(ls -A "${ARCHIVE}" 2>/dev/null)" ]; then
	echo "recover: REFUSING — ${ARCHIVE} is not empty." >&2
	echo "recover: Certbot holds real certificate generations for this name." >&2
	exit 1
fi

orphan_renewal=0
if [ -f "${RENEWAL}" ]; then
	orphan_renewal=1
fi

if [ "${legacy_live}" -eq 0 ] && [ "${orphan_renewal}" -eq 0 ]; then
	echo "recover: nothing to do — no legacy stopgap and no orphan renewal config"
	exit 0
fi

mkdir -p "${QUARANTINE}"

if [ "${legacy_live}" -eq 1 ]; then
	echo "recover: quarantining the legacy stopgap directory"
	mv "${LIVE}" "${QUARANTINE}/live-${ARENA64_DOMAIN}"
fi

if [ "${orphan_renewal}" -eq 1 ]; then
	# Moved rather than deleted: it records which ACME account and webroot
	# the failed attempt used, which is worth having if the next attempt
	# also fails.
	echo "recover: quarantining the orphan renewal config"
	mv "${RENEWAL}" "${QUARANTINE}/renewal-${ARENA64_DOMAIN}.conf"
fi

echo "recover: quarantined under ${QUARANTINE}"
echo "recover: ${LIVE} is now free for Certbot to create its own lineage"
echo "recover: next, run a STAGING dry run before spending a real rate-limit slot:"
echo "recover:   certbot certonly --webroot --webroot-path /var/www/certbot \\"
echo "recover:     --dry-run --cert-name ${ARENA64_DOMAIN} \\"
echo "recover:     -d ${ARENA64_DOMAIN} -d www.${ARENA64_DOMAIN} -d admin.${ARENA64_DOMAIN}"
exit 0
