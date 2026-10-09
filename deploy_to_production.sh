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
#   2. One-time repair of group-write permissions on any installed package
#      that embeds its own nested pixi project (aviary, binchicken, ...).
#      These are extracted by conda/pip under the default umask (022), so
#      despite mqpixi's directories being setgid, group members other than
#      whoever first built the env can't write into them - which is exactly
#      what breaks `aviary build`'s own `pixi install -a` for everyone else.
#      (pixi.toml's postinstall tasks now run under `umask 002`, so this is
#      only needed to fix already-built envs; new ones won't need it.)
#   3. Runs `pixi run postinstall`, which chains every env's postinstall task.
#
# Usage: deploy_to_production.sh [path-to-production-checkout]
# Defaults to /work/microbiome/sw/hpc_scripts.

set -euo pipefail

PRODUCTION_REPO="${1:-/work/microbiome/sw/hpc_scripts}"
MQPIXI_DIR="$PRODUCTION_REPO/mqpixi"
PIXI_ENVS_DIR="$MQPIXI_DIR/.pixi/envs"

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

echo "==> Repairing group-write permissions on nested pixi envs"
if [[ -d "$PIXI_ENVS_DIR" ]]; then
    found_any=0
    while IFS= read -r -d '' nested_pixi; do
        found_any=1
        pkg_dir="$(dirname "$nested_pixi")"
        echo "  chmod -R g+w $pkg_dir"
        chmod -R g+w "$pkg_dir"
    done < <(find "$PIXI_ENVS_DIR" -type d -name ".pixi" -print0)
    if [[ "$found_any" -eq 0 ]]; then
        echo "  (no installed packages with an embedded .pixi found, nothing to do)"
    fi
else
    echo "WARNING: $PIXI_ENVS_DIR not found (no envs built yet?), skipping" >&2
fi

echo "==> Running mqpixi postinstall"
cd "$MQPIXI_DIR"
pixi run postinstall
