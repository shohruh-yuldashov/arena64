#!/bin/sh
# The ownership boundary between Arena64 and Certbot — A64-030.2.
#
# Sourced by `issue.sh`, `renew.sh` and `recover-legacy-stopgap.sh` so that
# all three answer "is there a real certificate?" the same way. It is one
# file because three subtly different answers is how the incident below
# happened in the first place.
#
# ## The rule this file exists to enforce
#
#   /etc/letsencrypt/live      Certbot's. Arena64 NEVER creates anything here.
#   /etc/letsencrypt/archive   Certbot's.
#   /etc/letsencrypt/renewal   Certbot's.
#   /etc/letsencrypt/arena64   Arena64's. Certbot never reads or writes it.
#
# ## The incident
#
# The first production issuance reached Let's Encrypt, validated all three
# names, finalised the order and **received a real certificate** — then threw
# it away:
#
#     certbot.errors.CertStorageError: live directory exists for arena64.gg
#
# `RenewableCert.new_lineage` refuses to create a lineage when
# `live/<name>` already exists and is non-empty, and the old bootstrap wrote
# its self-signed stopgap directly into that directory. So the stopgap that
# exists to let nginx start was also the thing that made issuance impossible
# — permanently, on every retry.
#
# Verified against certbot v5.8.0's own storage module, offline:
#
#   clean                             -> lineage created
#   empty live/<d>/                   -> lineage created (guard tests listdir)
#   stopgap files in live/<d>/        -> BLOCKED, "live directory exists"
#   stopgap + orphan renewal conf     -> allocates <d>-0001, WRONG NAME
#
# The last row is why `recover-legacy-stopgap.sh` also quarantines the
# orphan renewal file: `util.unique_lineage_name` creates `<d>.conf` as a
# side effect *before* the guard runs, so a failed attempt leaves one
# behind, and the next attempt silently renames the lineage rather than
# failing loudly.

#: Where Arena64 keeps everything of its own. Never under live/ or archive/.
ARENA64_STATE="/etc/letsencrypt/arena64"

#: The stable path nginx reads. A symlink to either the stopgap or Certbot's
#: lineage — nginx never learns which, and never needs reconfiguring.
arena64_current_link() {
	printf '%s/current/%s' "${ARENA64_STATE}" "$1"
}

arena64_stopgap_dir() {
	printf '%s/stopgap/%s' "${ARENA64_STATE}" "$1"
}

certbot_live_dir() {
	printf '/etc/letsencrypt/live/%s' "$1"
}

# Whether Certbot holds a real lineage for this name.
#
# **Evidence, not a marker Arena64 wrote.** Certbot's `live/<name>/*.pem` are
# always symlinks into `archive/<name>/`; a stopgap written by this
# repository is a regular file. Testing the link — and where it resolves to
# — cannot be satisfied by anything Arena64 creates, which is exactly the
# property the old `.self-signed` marker lacked.
has_certbot_lineage() {
	_domain="$1"
	_full="$(certbot_live_dir "${_domain}")/fullchain.pem"
	[ -L "${_full}" ] || return 1
	[ -f "${_full}" ] || return 1
	_resolved="$(readlink -f "${_full}" 2>/dev/null)" || return 1
	case "${_resolved}" in
		/etc/letsencrypt/archive/"${_domain}"/*) return 0 ;;
		*) return 1 ;;
	esac
}

# Point the stable path at `$2`, atomically.
#
# `ln -sfn` onto an existing symlink is not atomic — it unlinks and
# recreates, and nginx reading in that window sees nothing. Creating a
# temporary link and renaming it over the old one is atomic.
#
# **`mv -T` and not `mv -f`.** The link being replaced points at a
# *directory*, and `mv -f new old` follows it: instead of replacing the
# symlink, `mv` moves `new` **inside** the directory `old` resolves to,
# leaves `old` pointing where it always did, and exits zero. The first draft
# of this function did exactly that — `issue.sh` logged "issuance complete"
# while nginx carried on serving the stopgap, and the only trace was a
# stray `.next` link inside the stopgap directory. `-T`
# (`--no-target-directory`) is what makes the destination a name rather than
# a directory to move into.
#
# The fallback keeps this working if `mv` ever lacks `-T`: `ln -sfn` handles
# the symlink-to-directory case correctly because `-n` refuses to follow it.
# It is not atomic, which is why it is second.
arena64_point_current_at() {
	_domain="$1"
	_target="$2"
	_link="$(arena64_current_link "${_domain}")"
	mkdir -p "$(dirname "${_link}")"
	ln -sfn "${_target}" "${_link}.next"
	if ! mv -T "${_link}.next" "${_link}" 2>/dev/null; then
		rm -f "${_link}.next"
		ln -sfn "${_target}" "${_link}"
	fi
}

# Where the stable path currently resolves to, for logging. Never a key.
arena64_current_target() {
	readlink "$(arena64_current_link "$1")" 2>/dev/null || echo "(unset)"
}
