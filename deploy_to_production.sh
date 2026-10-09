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
#   3. [rremoved by commenting out] Grants the microbiome-admin group write access (on top of the
#      microbiome group's existing read/execute) on anything the invoking
#      user owns under the shared pixi envs. conda/pip/pixi extract package
#      files owned by whoever happens to run the install, group=microbiome,
#      mode 755 - fine for regular read-only use, but it means only that one
#      person can write there afterwards (e.g. aviary's/binchicken's own
#      nested `pixi install -a`, or write_activate_vars.py touching an env
#      someone else built first). A plain `chmod g+w` can't fix this without
#      also handing write access to every ordinary microbiome member, since
#      microbiome is the *owning* group - two different groups needing two
#      different privilege levels on the same files requires a POSIX ACL.
#      Setting the *default* ACL (-d) means anything created under these
#      directories from now on inherits it automatically, so this only ever
#      needs to run again for directories that don't have it yet; it's
#      scoped to `$(id -un)`'s own files so running it as a different admin
#      never fails on paths owned by someone else.
#   4. Runs `pixi run postinstall`, which chains every env's postinstall task.
#
# Usage: deploy_to_production.sh [path-to-production-checkout]
# Defaults to /work/microbiome/sw/hpc_scripts.

set -euo pipefail

PRODUCTION_REPO="${1:-/work/microbiome/sw/hpc_scripts}"
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

# echo "==> Granting $ADMIN_GROUP write access on anything owned by $(id -un)"
# if [[ -d "$PIXI_ENVS_DIR" ]]; then
#     # -m: existing files/dirs the current user owns get the ACL now.
#     # -d: directories also get it as a *default* ACL, so anything created
#     # under them later (by this user, with any umask) inherits it too.
#     find "$PIXI_ENVS_DIR" -user "$(id -un)" \( -type d -o -type f \) -print0 \
#         | xargs -0 -r setfacl -m "g:${ADMIN_GROUP}:rwx"
#     find "$PIXI_ENVS_DIR" -user "$(id -un)" -type d -print0 \
#         | xargs -0 -r setfacl -d -m "g:${ADMIN_GROUP}:rwx"
# else
#     echo "WARNING: $PIXI_ENVS_DIR not found (no envs built yet?), skipping" >&2
# fi

echo "==> Running mqpixi postinstall"
cd "$MQPIXI_DIR"
pixi run postinstall
