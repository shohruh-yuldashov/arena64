#!/bin/sh
# One-time recovery for a host that ran the pre-A64-030.2 bootstrap.
#
#   docker compose --env-file production.env run --rm --no-deps \
#     --entrypoint sh certbot /usr/local/bin/recover-legacy-stopgap.sh
#
# **`--no-deps` is not optional.** The `certbot` service declares
# `depends_on: certbot-init`, so without it Compose runs `certbot-init` —
# and therefore `issue.sh`, and therefore a real Let's Encrypt request —
# *before* this script gets to clean anything up. That is not hypothetical:
# it is how the production host acquired a lineage called
# `arena64.gg-0001`. `deployment.md` §8.12 tells the story.
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
# — quietly — and produces a certificate the edge did not read until
# A64-030.3 taught it to look.
#
# Both behaviours were verified against certbot v5.8.0's own storage module
# offline, and `tests/unit/test_acme_bootstrap.py` reproduces them.
#
# ## What it refuses
#
# Anything that looks like a real certificate, and anything it cannot read
# unambiguously. A Certbot lineage is symlinks under `live/` resolving into
# `archive/`; if that is what is under `<domain>`, this script exits
# non-zero and changes nothing. It also refuses when the legacy fingerprint
# is absent, when an archive lineage exists, when the renewal file is not
# the empty orphan it expects, or when discovery finds more than one valid
# lineage — each of those means the state is not what this script was
# written for, and guessing with somebody's private key is not a thing to
# do.
#
# **Numbered lineages are never touched.** `live/<domain>-0001` and its
# siblings are genuine Certbot lineages; this script names exactly two paths
# and neither of them can be one. It reports what it is leaving alone so
# that an operator can see the same thing.
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

# --- What must survive untouched --------------------------------------------
#
# Printed before anything moves so the log records what this run considered
# real, not just what it moved.
for candidate in $(arena64_candidate_lineage_names "${ARENA64_DOMAIN}"); do
	if [ "${candidate}" != "${ARENA64_DOMAIN}" ] && arena64_lineage_is_well_formed "${candidate}"; then
		echo "recover: leaving the genuine Certbot lineage ${candidate} untouched"
	fi
done

# --- Refusal 1: a real certificate is present under this exact name ---------
if has_certbot_lineage "${ARENA64_DOMAIN}"; then
	echo "recover: REFUSING — ${LIVE} is a real Certbot lineage." >&2
	echo "recover: This host has a working certificate. Nothing to recover." >&2
	exit 1
fi

# --- Refusal 2: discovery cannot say which lineage is canonical -------------
#
# Two or more valid lineages for these names means somebody has to decide
# which one the edge should serve, and moving state around underneath that
# decision only makes it harder.
arena64_discover_lineage "${ARENA64_DOMAIN}" >/dev/null 2>&1 && discovery=0 || discovery=$?
if [ "${discovery}" -eq "${ARENA64_LINEAGE_AMBIGUOUS}" ]; then
	echo "recover: REFUSING — more than one valid Certbot lineage matches ${ARENA64_DOMAIN}." >&2
	echo "recover: An operator must retire the ones that are not canonical first." >&2
	exit 1
fi

legacy_live=0
if [ -d "${LIVE}" ]; then
	# --- Refusal 3: present but not the shape this script understands -------
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

# --- Refusal 4: an archive lineage exists under this exact name -------------
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
	# --- Refusal 5: a renewal config with content is not the orphan ---------
	#
	# The orphan is zero bytes because `safe_open` creates it before anything
	# is written to it. A file with content describes a lineage — one whose
	# `live/` and `archive/` the refusals above have already found missing,
	# which is a state nobody has seen and this script will not improvise on.
	if [ -s "${RENEWAL}" ]; then
		echo "recover: REFUSING — ${RENEWAL} is not empty." >&2
		echo "recover: The orphan this script removes is a zero-byte file; this one describes a lineage." >&2
		exit 1
	fi
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
echo "recover:   docker compose --env-file production.env run --rm --no-deps \\"
echo "recover:     --entrypoint certbot certbot certonly \\"
echo "recover:     --webroot --webroot-path /var/www/certbot --dry-run \\"
echo "recover:     --cert-name ${ARENA64_DOMAIN} \\"
echo "recover:     -d ${ARENA64_DOMAIN} -d www.${ARENA64_DOMAIN} -d admin.${ARENA64_DOMAIN}"
exit 0
