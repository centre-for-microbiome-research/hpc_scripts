#!/bin/bash
# deploy_to_production.sh - safely sync the production mqpixi checkout with
# git and (re)run its postinstall tasks.
#
# Must be run OUTSIDE the mqyolo/opencode sandbox: the production checkout
# under /work and the shared pixi envs under /pkg are read-only inside it.
#
# What it does:
#   1. git pull --ff-only in the production checkout (refuses to run at all
#      if there are local changes, and refuses to pull if that would create
#      a merge commit - this is a shared checkout, not a dev sandbox).
#   2. `pixi install -a --frozen`, installing every one of the ~100+ tool
#      environments defined in the manifest (not just the ones postinstall
#      touches), so non-admin users never trigger a slow first-time install
#      (or hit a permissions error building one) themselves.
#   3. Only with --fix-permissions: fixes permissions on the shared pixi envs
#      (see fix_env_permissions below): group $ADMIN_GROUP gets read/write/execute, everyone else
#      (including ordinary microbiome members, who are not in $ADMIN_GROUP)
#      gets read, plus execute where the owner/group have it, but never write.
#      conda/pip/pixi extract files as whoever runs the install, mode 660/770
#      with no access for "other", which is why `mqpixi aviary` failed for
#      anyone outside the owning group. /pkg (weka) does not take the POSIX ACLs
#      that would let microbiome-admin be a *second* group, so $ADMIN_GROUP is
#      made the owning group and "other" carries the read-only access.
#      Re-run after the postinstall step too, since that creates files.
#   4. Runs `pixi run postinstall`, which chains every env's postinstall task.
#
# Usage: deploy_to_production.sh [--fix-permissions] [path-to-production-checkout]
# Defaults to /work/microbiome/sw/hpc_scripts.

set -euo pipefail

FIX_PERMISSIONS=0
POSITIONAL=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --fix-permissions) FIX_PERMISSIONS=1 ;;
        -h|--help)
            sed -n '/^# Usage:/,/^$/p' "$0" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        -*) echo "ERROR: unknown option: $1" >&2; exit 1 ;;
        *) POSITIONAL+=("$1") ;;
    esac
    shift
done
if [[ ${#POSITIONAL[@]} -gt 1 ]]; then
    echo "ERROR: expected at most one production checkout path" >&2
    exit 1
fi

PRODUCTION_REPO="${POSITIONAL[0]:-/work/microbiome/sw/hpc_scripts}"
MQPIXI_DIR="$PRODUCTION_REPO/mqpixi"
PIXI_ENVS_DIR="$MQPIXI_DIR/.pixi/envs"
ADMIN_GROUP="${ADMIN_GROUP:-microbiome-admin}"

if [[ ! -d "$PRODUCTION_REPO/.git" ]]; then
    echo "ERROR: $PRODUCTION_REPO is not a git checkout" >&2
    exit 1
fi

if [[ ! -w "$PRODUCTION_REPO" ]]; then
    echo "ERROR: $PRODUCTION_REPO is not writable. Are you running inside the" >&2
    echo "mqyolo/opencode sandbox? /work and /pkg are read-only in there - run" >&2
    echo "this script from a normal login shell instead." >&2
    exit 1
fi

echo "==> Updating $PRODUCTION_REPO"
cd "$PRODUCTION_REPO"

# Refuse to pull over local modifications/untracked changes - this is a
# shared production checkout, not a dev sandbox.
if [[ -n "$(git status --porcelain)" ]]; then
    echo "ERROR: $PRODUCTION_REPO has local changes or untracked files; refusing" >&2
    echo "to pull. Run 'git status' there and resolve first." >&2
    exit 1
fi

git fetch --quiet

# --ff-only: fail loudly rather than silently create a merge commit on the
# shared production clone if it has diverged from upstream.
if ! git pull --ff-only; then
    echo "ERROR: 'git pull --ff-only' failed (local branch has diverged from" >&2
    echo "upstream). Resolve manually in $PRODUCTION_REPO before re-running." >&2
    exit 1
fi

echo "==> Installing all environments"
cd "$MQPIXI_DIR"
pixi install -a --frozen

fix_env_permissions() {
    if [[ "$FIX_PERMISSIONS" -ne 1 ]]; then
        return 0
    fi
    echo "==> Setting $ADMIN_GROUP:rwx, others:r-x on $PIXI_ENVS_DIR"
    if [[ ! -d "$PIXI_ENVS_DIR" ]]; then
        echo "WARNING: $PIXI_ENVS_DIR not found (no envs built yet?), skipping" >&2
        return 0
    fi
    # Directory above the envs: others need execute (traverse) to reach them.
    # Not recursive - .git etc. in there are not meant for everyone.
    local envs_root
    envs_root="$(dirname "$(realpath "$PIXI_ENVS_DIR")")/.."
    chmod o+rx "$envs_root" || echo "WARNING: could not chmod o+rx $envs_root" >&2
    # mpermissions keeps going past failures on files owned by someone else.
    "$PRODUCTION_REPO/bin/mpermissions" -g "$ADMIN_GROUP" --other-read "$(realpath "$PIXI_ENVS_DIR")"
}

fix_env_permissions

echo "==> Running mqpixi postinstall"
cd "$MQPIXI_DIR"
pixi run postinstall

fix_env_permissions
