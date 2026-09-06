#!/bin/sh
# The ownership boundary between Arena64 and Certbot — A64-030.2, A64-030.3.
#
# Sourced by `issue.sh`, `renew.sh` and `recover-legacy-stopgap.sh` so that
# all three answer "is there a real certificate, and where is it?" the same
# way. It is one file because three subtly different answers is how the
# incidents below happened in the first place.
#
# ## The rule this file exists to enforce
#
#   /etc/letsencrypt/live      Certbot's. Arena64 NEVER creates anything here.
#   /etc/letsencrypt/archive   Certbot's.
#   /etc/letsencrypt/renewal   Certbot's.
#   /etc/letsencrypt/arena64   Arena64's. Certbot never reads or writes it.
#
# ## Incident 1 — the stopgap in Certbot's namespace
#
# The first production issuance reached Let's Encrypt, validated all three
# names, finalised the order and **received a real certificate** — then threw
# it away:
#
#     certbot.errors.CertStorageError: live directory exists for arena64.gg
#
# `RenewableCert.new_lineage` refuses to create a lineage when
# `live/<name>` already exists and is non-empty, and the old bootstrap wrote
# its self-signed stopgap directly into that directory.
#
# ## Incident 2 — the lineage Certbot actually created
#
# The recovery for incident 1 was run without `--no-deps`, so Compose started
# `certbot-init` first (`certbot` depends on it) and a second real issuance
# ran while the orphan `renewal/arena64.gg.conf` was still in place.
# `util.unique_lineage_name` found the name taken and returned
# `arena64.gg-0001.conf`, so Certbot created its lineage at
# **`live/arena64.gg-0001`** — a real, trusted, correctly-stored certificate
# at a path nothing in this repository looked at.
#
# Every script then reported "no lineage" for ever, because they all asked
# the same wrong question: *does `live/$ARENA64_DOMAIN` exist?* Certbot never
# promised that. The lineage name is whatever `unique_lineage_name` returned,
# and `-0001` is a perfectly ordinary answer.
#
# So state is now **discovered**, not assumed: `arena64_discover_lineage`
# enumerates the lineage names Certbot could plausibly have used for this
# certificate and validates each one against Certbot-managed evidence.
#
# Verified against certbot v5.8.0's own storage module, offline:
#
#   clean                             -> lineage created as <domain>
#   empty live/<d>/                   -> lineage created (guard tests listdir)
#   stopgap files in live/<d>/        -> BLOCKED, "live directory exists"
#   stopgap + orphan renewal conf     -> allocates <d>-0001
#   orphan renewal conf alone         -> allocates <d>-0001
#
# ## Fail closed, never guess
#
# Discovery reports four states, and only one of them may lead to an ACME
# request. Ambiguous and malformed states stop the lifecycle and say so:
# spending a duplicate-certificate slot to resolve confusion is how both
# incidents above cost one.

#: Where Arena64 keeps everything of its own. Never under live/ or archive/.
ARENA64_STATE="/etc/letsencrypt/arena64"

#: `arena64_discover_lineage` exit statuses. Named because all three callers
#: branch on every one of them, and a bare `3` at those call sites is
#: unreadable.
ARENA64_LINEAGE_FOUND=0
ARENA64_LINEAGE_NONE=1
ARENA64_LINEAGE_AMBIGUOUS=2
ARENA64_LINEAGE_MALFORMED=3

#: The stable path nginx reads. A symlink to either the stopgap or Certbot's
#: lineage — nginx never learns which, and never needs reconfiguring.
arena64_current_link() {
	printf '%s/current/%s' "${ARENA64_STATE}" "$1"
}

arena64_stopgap_dir() {
	printf '%s/stopgap/%s' "${ARENA64_STATE}" "$1"
}

# The directory Certbot keeps a lineage's symlinks in.
#
# **Takes a lineage name, not a domain.** They coincide on a host that has
# only ever issued cleanly; incident 2 is what happens when code assumes they
# always do.
certbot_live_dir() {
	printf '/etc/letsencrypt/live/%s' "$1"
}

# Whether `live/<name>` is Certbot's own layout.
#
# **Evidence, not a marker Arena64 wrote.** Certbot's `live/<name>/*.pem` are
# always symlinks into `archive/<name>/`; a stopgap written by this
# repository is a regular file. Testing the link — and where it resolves to
# — cannot be satisfied by anything Arena64 creates, which is exactly the
# property the old `.self-signed` marker lacked.
#
# Deliberately the *weakest* of the three checks in this file: recovery uses
# it to decide what it must not touch, and there the safe answer to "is this
# possibly a real certificate?" is yes on the slightest evidence.
has_certbot_lineage() {
	_hcl_name="$1"
	_hcl_full="$(certbot_live_dir "${_hcl_name}")/fullchain.pem"
	[ -L "${_hcl_full}" ] || return 1
	[ -f "${_hcl_full}" ] || return 1
	_hcl_resolved="$(readlink -f "${_hcl_full}" 2>/dev/null)" || return 1
	case "${_hcl_resolved}" in
		/etc/letsencrypt/archive/"${_hcl_name}"/*) return 0 ;;
		*) return 1 ;;
	esac
}

# Every lineage name Certbot could have used for this certificate.
#
# `unique_lineage_name` appends `-0001`, `-0002`, … when the base name is
# taken, so the candidates are `<domain>` and `<domain>-<digits>` — and
# nothing else, because a lineage under an unrelated name is not ours to
# reason about.
#
# Names are collected from **both** `live/` and `renewal/`: a lineage has an
# entry in each, and a candidate that appears in only one is precisely the
# broken shape discovery has to notice rather than skip.
arena64_candidate_lineage_names() {
	_cand_domain="$1"
	_cand_seen=" "
	for _cand_path in /etc/letsencrypt/live/*/ /etc/letsencrypt/renewal/*.conf; do
		[ -e "${_cand_path}" ] || continue
		_cand_name="${_cand_path%/}"
		_cand_name="${_cand_name##*/}"
		_cand_name="${_cand_name%.conf}"

		case "${_cand_name}" in
			"${_cand_domain}") ;;
			"${_cand_domain}"-*)
				_cand_suffix="${_cand_name#"${_cand_domain}"-}"
				case "${_cand_suffix}" in
					'' | *[!0-9]*) continue ;;
				esac
				;;
			*) continue ;;
		esac

		case "${_cand_seen}" in
			*" ${_cand_name} "*) continue ;;
		esac
		_cand_seen="${_cand_seen}${_cand_name} "
	done
	printf '%s' "${_cand_seen}"
}

# Whether `<name>` is stored the way Certbot stores a lineage it maintains.
#
# Stricter than `has_certbot_lineage` because this one decides whether the
# renewal loop may rely on the lineage: all three files nginx needs must
# resolve into `archive/<name>/`, and `renewal/<name>.conf` must exist and
# carry content.
#
# **The non-empty test is the orphan test.** `unique_lineage_name` creates
# `<name>.conf` with `safe_open` *before* the live-directory guard runs and
# does not unlink it when the guard raises, so a zero-byte renewal config is
# the fingerprint of a failed attempt — not of a certificate.
arena64_lineage_is_well_formed() {
	_str_name="$1"
	_str_live="$(certbot_live_dir "${_str_name}")"

	for _str_file in fullchain.pem privkey.pem chain.pem; do
		[ -L "${_str_live}/${_str_file}" ] || return 1
		[ -f "${_str_live}/${_str_file}" ] || return 1
		_str_resolved="$(readlink -f "${_str_live}/${_str_file}" 2>/dev/null)" || return 1
		case "${_str_resolved}" in
			/etc/letsencrypt/archive/"${_str_name}"/*) ;;
			*) return 1 ;;
		esac
	done

	[ -s "/etc/letsencrypt/renewal/${_str_name}.conf" ] || return 1
	return 0
}

# Whether the certificate stored under `<name>` is the one Arena64 asked for.
#
# A well-formed lineage under one of our candidate names could still be
# somebody else's certificate — a single-name cert left by an experiment, or
# one issued before `www` and `admin` were added. Serving it would produce a
# name-mismatch error on two of three hosts, which under
# `includeSubDomains; preload` is not something a visitor can click through.
#
# Reads only the leaf certificate, which is public. No private material is
# opened, and nothing is printed.
arena64_lineage_covers_identity() {
	_cov_name="$1"
	_cov_domain="$2"
	_cov_leaf="$(certbot_live_dir "${_cov_name}")/fullchain.pem"

	# `sed` and not `tr -d`: the names must stay one per line for the
	# fixed-string match below, and `tr -d '[:space:]'` would delete the
	# newlines too, concatenating three names into one that matches nothing.
	_cov_names="$(
		openssl x509 -in "${_cov_leaf}" -noout -ext subjectAltName 2>/dev/null |
			tr ',' '\n' |
			sed -n 's/.*DNS://p' |
			sed 's/[[:space:]]//g'
	)" || return 1

	for _cov_want in "${_cov_domain}" "www.${_cov_domain}" "admin.${_cov_domain}"; do
		printf '%s\n' "${_cov_names}" | grep -Fxq "${_cov_want}" || return 1
	done
	return 0
}

# Find the one Certbot lineage this deployment should be serving.
#
# Prints the lineage directory on stdout and returns `ARENA64_LINEAGE_FOUND`
# when exactly one candidate is well formed and carries the right identity.
# Otherwise prints nothing and returns one of the other three statuses, with
# an explanation on stderr.
#
# **A malformed candidate outranks a valid one.** A host holding both a good
# lineage and wreckage under a sibling name is a host where nobody can say
# which certificate is canonical without looking — and the two ways to be
# wrong are to renew the wrong one or to buy a third. Stopping costs a
# warning in the log; guessing costs a rate-limit slot and possibly the edge.
arena64_discover_lineage() {
	_disc_domain="$1"
	_disc_valid=''
	_disc_count=0
	_disc_malformed=0

	for _disc_name in $(arena64_candidate_lineage_names "${_disc_domain}"); do
		if ! arena64_lineage_is_well_formed "${_disc_name}"; then
			_disc_malformed=1
			echo "lineage: ${_disc_name} occupies Certbot's namespace but is not a usable lineage" >&2
			continue
		fi
		if ! arena64_lineage_covers_identity "${_disc_name}" "${_disc_domain}"; then
			_disc_malformed=1
			echo "lineage: ${_disc_name} is a Certbot lineage but does not cover ${_disc_domain}, www and admin" >&2
			continue
		fi
		_disc_valid="${_disc_valid} ${_disc_name}"
		_disc_count=$((_disc_count + 1))
	done

	if [ "${_disc_malformed}" -eq 1 ]; then
		echo "lineage: refusing to act on a malformed certificate state — run recover-legacy-stopgap.sh or inspect by hand" >&2
		return "${ARENA64_LINEAGE_MALFORMED}"
	fi
	if [ "${_disc_count}" -eq 0 ]; then
		return "${ARENA64_LINEAGE_NONE}"
	fi
	if [ "${_disc_count}" -gt 1 ]; then
		echo "lineage: ${_disc_count} valid lineages match ${_disc_domain}:${_disc_valid}" >&2
		echo "lineage: refusing to choose one — an operator must retire the others" >&2
		return "${ARENA64_LINEAGE_AMBIGUOUS}"
	fi

	# Exactly one, and the loop above left its name with a leading space.
	certbot_live_dir "${_disc_valid# }"
	return "${ARENA64_LINEAGE_FOUND}"
}

# The status as a word, for log lines an operator reads at 3am.
arena64_lineage_status_name() {
	case "$1" in
		"${ARENA64_LINEAGE_FOUND}") printf 'FOUND' ;;
		"${ARENA64_LINEAGE_NONE}") printf 'NONE' ;;
		"${ARENA64_LINEAGE_AMBIGUOUS}") printf 'AMBIGUOUS' ;;
		"${ARENA64_LINEAGE_MALFORMED}") printf 'MALFORMED' ;;
		*) printf 'UNKNOWN(%s)' "$1" ;;
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
	_pca_domain="$1"
	_pca_target="$2"
	_pca_link="$(arena64_current_link "${_pca_domain}")"
	mkdir -p "$(dirname "${_pca_link}")"
	ln -sfn "${_pca_target}" "${_pca_link}.next"
	if ! mv -T "${_pca_link}.next" "${_pca_link}" 2>/dev/null; then
		rm -f "${_pca_link}.next"
		ln -sfn "${_pca_target}" "${_pca_link}"
	fi
}

# Whether nginx would find a certificate through the stable path right now.
#
# The question a caller asks before replacing what `current` points at: a
# link that already resolves to three readable files is serving the edge,
# and repointing it at a fresh stopgap would downgrade a working site.
arena64_current_is_usable() {
	_cur_link="$(arena64_current_link "$1")"
	for _cur_file in fullchain.pem privkey.pem chain.pem; do
		[ -f "${_cur_link}/${_cur_file}" ] || return 1
	done
	return 0
}

# Where the public half of the certificate is published for readers that are
# not root — A64-030.4B.1 (B-1).
#
# Certbot's `archive/` is `0700 root:root` because it holds private keys, and
# every path to a certificate resolves through it: `live/<name>/*.pem` and
# `arena64/current/<domain>/*.pem` are both symlinks into that directory. So
# the worker, which runs as uid 10001 and needs nothing but the expiry date,
# could not read the certificate at all and
# `arena64_certificate_expiry_timestamp_seconds` was never published —
# leaving `CertificateMissing`, `CertificateExpiringSoon` and
# `CertificateExpired` unable to fire.
arena64_observability_dir() {
	printf '%s/observability/%s' "${ARENA64_STATE}" "$1"
}

# Publish the public certificate the edge is currently serving.
#
# **Public material only.** It copies `fullchain.pem` — the leaf and its
# chain, which every client already receives during the handshake — and
# nothing else. `privkey.pem` is never read, never copied and never named
# here, and the guard below refuses to publish a file that turns out to
# contain a key rather than trusting that it never will.
#
# **Sourced through `arena64/current/<domain>`**, which is the path nginx
# reads, so the metric and the edge cannot disagree about which certificate
# is live. That includes the stopgap: a host serving a three-day self-signed
# certificate should have an expiry metric that says so.
#
# **Atomic.** Written to a temporary name and renamed over the target, so a
# reader sees either the previous certificate or the new one and never a
# half-written file.
#
# **Fails soft, and loudly.** Every failure path leaves the previous
# projection untouched: a stale last-known-good certificate is a metric that
# is wrong about the date, while a truncated one is a metric that cannot be
# read at all — and the alert that matters most is the one for a certificate
# nobody can see. Returns non-zero so a caller may log; no caller may treat
# it as fatal, because nginx serving TLS must never depend on whether an
# observability copy was written.
arena64_project_public_certificate() {
	_proj_domain="$1"
	_proj_source="$(arena64_current_link "${_proj_domain}")/fullchain.pem"
	_proj_dir="$(arena64_observability_dir "${_proj_domain}")"
	_proj_target="${_proj_dir}/fullchain.pem"
	_proj_tmp="${_proj_target}.next"

	if [ ! -f "${_proj_source}" ]; then
		echo "certbot: ${_proj_source} is not readable; observability certificate left as it was" >&2
		return 1
	fi

	mkdir -p "${_proj_dir}" || {
		echo "certbot: cannot create ${_proj_dir}; observability certificate left as it was" >&2
		return 1
	}
	# Explicit rather than umask-dependent: the whole point of this file is
	# that a uid which is not root can read it.
	chmod 0755 "${ARENA64_STATE}/observability" "${_proj_dir}" 2>/dev/null || true

	rm -f "${_proj_tmp}"
	if ! cp "${_proj_source}" "${_proj_tmp}"; then
		rm -f "${_proj_tmp}"
		echo "certbot: could not copy ${_proj_source}; observability certificate left as it was" >&2
		return 1
	fi

	if ! grep -q -- '-----BEGIN CERTIFICATE-----' "${_proj_tmp}"; then
		rm -f "${_proj_tmp}"
		echo "certbot: ${_proj_source} does not look like a certificate; refusing to publish it" >&2
		return 1
	fi
	# Belt and braces. `fullchain.pem` cannot contain a key, and this is what
	# makes that a checked property rather than an assumption.
	if grep -q -- 'PRIVATE KEY' "${_proj_tmp}"; then
		rm -f "${_proj_tmp}"
		echo "certbot: refusing to publish a file containing private key material" >&2
		return 1
	fi

	chmod 0444 "${_proj_tmp}"
	if ! mv -f "${_proj_tmp}" "${_proj_target}"; then
		rm -f "${_proj_tmp}"
		echo "certbot: could not replace ${_proj_target}; observability certificate left as it was" >&2
		return 1
	fi
	return 0
}

# Where the stable path currently resolves to, for logging. Never a key.
arena64_current_target() {
	readlink "$(arena64_current_link "$1")" 2>/dev/null || echo "(unset)"
}
