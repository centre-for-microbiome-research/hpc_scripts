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
#      Also makes pixi's .pixi/task-cache-v0 writable by $ADMIN_GROUP (see
#      fix_task_cache_permissions).
#      Runs before the install (which needs write access to every env), and
#      again after the install and postinstall steps, since those create files.
#      This is retrospective only: the script runs the install under umask 002,
#      so envs and cache files created by a normal run are already group-writable.
#      mpermissions is run per env rather than once over the whole envs dir, and
#      an env that comes back clean (no "CMD FAILED" in its output - mpermissions
#      itself always exits 0, see fix_env_permissions) gets a permissions.done
#      marker written at its root so future --fix-permissions runs skip it,
#      rather than re-walking every file of ~100+ envs every time. An env left
#      with some other-owned files has no marker written, so it's retried on
#      every run until whoever owns those files runs this themselves. Delete an
#      env's permissions.done to force it to be re-checked, or use
#      --fix-permissions-env NAME (repeatable), which implies --fix-permissions,
#      restricts this per-env step to the named env(s) and always re-checks
#      them, ignoring (and refreshing) their markers. The task-cache fix and the
#      activation-script fix below still cover everything.
#   4. Runs `pixi run postinstall`, which chains every env's postinstall task.
#   Every run (with or without --fix-permissions), after steps 2 and 4, makes
#   each env's etc/conda/{activate,deactivate,env_vars}.d scripts owned by
#   $ADMIN_GROUP and readable by everyone, and the directories above them
#   traversable (see
#   fix_activation_permissions); packages ship e.g. mode-660 scripts that
#   otherwise break `conda activate` for ordinary users.
#
# Usage: deploy_to_production.sh [--fix-permissions] [--fix-permissions-env NAME]...
#                                [path-to-production-checkout]
#   --fix-permissions          fix permissions on every env without a permissions.done marker
#   --fix-permissions-env NAME fix permissions on env NAME only, even if it has a marker
#                              (repeatable; implies --fix-permissions)
# Defaults to /work/microbiome/sw/hpc_scripts.

set -euo pipefail

FIX_PERMISSIONS=0
FIX_PERMISSIONS_ENVS=()
NOT_BEN=0
POSITIONAL=()
add_fix_permissions_env() {
    local name="${1%/}"
    if [[ -z "$name" || "$name" == */* || "$name" == .* ]]; then
        echo "ERROR: --fix-permissions-env expects an environment name (e.g. aviary-v0-12-0), got: '$1'" >&2
        exit 1
    fi
    FIX_PERMISSIONS=1
    FIX_PERMISSIONS_ENVS+=("$name")
}
while [[ $# -gt 0 ]]; do
    case "$1" in
        --fix-permissions) FIX_PERMISSIONS=1 ;;
        --fix-permissions-env)
            if [[ $# -lt 2 ]]; then
                echo "ERROR: --fix-permissions-env requires an environment name" >&2
                exit 1
            fi
            add_fix_permissions_env "$2"
            shift ;;
        --fix-permissions-env=*) add_fix_permissions_env "${1#*=}" ;;
        --not-ben) NOT_BEN=1 ;;
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

if [[ "$NOT_BEN" -eq 0 ]]; then
    # Production only gets what is on GitHub (the pull below), so deploying
    # from a checkout with unpushed work would silently leave it out.
    SCRIPT_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
    if git -C "$SCRIPT_REPO" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        echo "==> Checking $SCRIPT_REPO is pushed to GitHub"
        if [[ -n "$(git -C "$SCRIPT_REPO" status --porcelain --untracked-files=no)" ]]; then
            echo "ERROR: $SCRIPT_REPO has uncommitted changes to tracked files; commit" >&2
            echo "and push them before deploying." >&2
            exit 1
        fi
        git -C "$SCRIPT_REPO" fetch --quiet
        if ! UPSTREAM="$(git -C "$SCRIPT_REPO" rev-parse --abbrev-ref --symbolic-full-name '@{upstream}' 2>/dev/null)"; then
            echo "ERROR: the current branch of $SCRIPT_REPO has no upstream, so it has" >&2
            echo "not been pushed to GitHub. Push it before deploying." >&2
            exit 1
        fi
        UNPUSHED="$(git -C "$SCRIPT_REPO" rev-list --count "$UPSTREAM..HEAD")"
        if [[ "$UNPUSHED" -gt 0 ]]; then
            echo "ERROR: $SCRIPT_REPO has $UNPUSHED commit(s) not pushed to $UPSTREAM." >&2
            echo "Run 'git push' before deploying - production only pulls from GitHub." >&2
            exit 1
        fi
    fi

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
fi

# Pixi records every finished task as a file in .pixi/task-cache-v0 and fails
# with a bare "Permission denied (os error 13)" - after the task itself has
# succeeded - if it cannot create one. Created under a 022 umask the directory
# is not group-writable, so a second admin hits that on the first task that has
# no cache file yet (e.g. postinstall for a newly added env). Group is
# $ADMIN_GROUP, not the checkout's setgid group, because only deployers write
# here; setgid keeps files created later in that group.
fix_task_cache_permissions() {
    local cache_dir="$MQPIXI_DIR/.pixi/task-cache-v0"
    if [[ ! -d "$cache_dir" ]]; then
        return 0
    fi
    echo "==> Setting $ADMIN_GROUP:rw on $cache_dir"
    # Only the owner can change these; keep going past files owned by others.
    chgrp "$ADMIN_GROUP" "$cache_dir" || echo "WARNING: could not chgrp $cache_dir" >&2
    chmod g+rwxs "$cache_dir" || echo "WARNING: could not chmod $cache_dir" >&2
    find "$cache_dir" -maxdepth 1 -type f \
        \( ! -group "$ADMIN_GROUP" -o ! -perm -g=rw \) -print0 \
        | xargs -0 -r sh -c '
            for f; do
                chgrp "$0" "$f" && chmod g+rw "$f" \
                    || echo "WARNING: could not fix permissions on $f" >&2
            done' "$ADMIN_GROUP"
}

# Every conda/pixi activation reads the scripts in etc/conda/{activate,
# deactivate}.d (and env_vars.d) as whoever is activating, so one that "other"
# cannot read - or a directory above it that "other" cannot traverse - breaks
# the env for ordinary users. Ordinary microbiome members are not in
# $ADMIN_GROUP, so they get read via "other"; $ADMIN_GROUP owns the paths and
# has rw(x), with setgid on directories so later files stay in that group.
# umask cannot prevent this: package files keep the mode stored in the package
# (e.g. resfinder.sh is 660) and rattler creates the directories 770, so this
# runs on every deploy, not just --fix-permissions.
# Only a path owned by the invoking user can be changed; for anything else the
# owner has to run this script (the warning names the path).
fix_activation_permissions() {
    if [[ ! -d "$PIXI_ENVS_DIR" ]]; then
        return 0
    fi
    local p
    local -a dirs files
    shopt -s nullglob
    dirs=("$PIXI_ENVS_DIR"/*/ "$PIXI_ENVS_DIR"/*/etc "$PIXI_ENVS_DIR"/*/etc/conda
          "$PIXI_ENVS_DIR"/*/etc/conda/{activate,deactivate,env_vars}.d)
    files=("$PIXI_ENVS_DIR"/*/etc/conda/{activate,deactivate,env_vars}.d/*)
    shopt -u nullglob
    if [[ ${#dirs[@]} -gt 0 ]]; then
        while IFS= read -r -d '' p; do
            { chgrp "$ADMIN_GROUP" "$p" && chmod g+rwxs,o+rx "$p"; } \
                || echo "WARNING: could not fix permissions on $p" >&2
        done < <(find "${dirs[@]}" -maxdepth 0 -type d \
                      \( ! -group "$ADMIN_GROUP" -o ! -perm -g=rwxs -o ! -perm -o=rx \) -print0)
    fi
    if [[ ${#files[@]} -gt 0 ]]; then
        while IFS= read -r -d '' p; do
            { chgrp "$ADMIN_GROUP" "$p" && chmod g+rw,o+r "$p"; } \
                || echo "WARNING: could not fix permissions on $p" >&2
        done < <(find "${files[@]}" -maxdepth 0 -type f \
                      \( ! -group "$ADMIN_GROUP" -o ! -perm -g=rw -o ! -perm -o=r \) -print0)
    fi
}

fix_env_permissions() {
    if [[ "$FIX_PERMISSIONS" -ne 1 ]]; then
        return 0
    fi
    fix_task_cache_permissions
    if [[ ! -d "$PIXI_ENVS_DIR" ]]; then
        echo "WARNING: $PIXI_ENVS_DIR not found (no envs built yet?), skipping" >&2
        return 0
    fi
    # Directory above the envs: others need execute (traverse) to reach them.
    # Not recursive - .git etc. in there are not meant for everyone.
    local envs_root
    envs_root="$(dirname "$(realpath "$PIXI_ENVS_DIR")")/.."
    chmod o+rx "$envs_root" || echo "WARNING: could not chmod o+rx $envs_root" >&2

    local env_dir marker output name
    local -a env_dirs=()
    local forced=0
    if [[ ${#FIX_PERMISSIONS_ENVS[@]} -gt 0 ]]; then
        # Explicitly targeted: re-check even if a marker says it was clean.
        forced=1
        for name in "${FIX_PERMISSIONS_ENVS[@]}"; do
            if [[ -d "$PIXI_ENVS_DIR/$name" ]]; then
                env_dirs+=("$PIXI_ENVS_DIR/$name")
            else
                # Can legitimately be missing on the pass before the install.
                echo "WARNING: environment '$name' not found in $PIXI_ENVS_DIR (not installed yet?), skipping" >&2
            fi
        done
    else
        shopt -s nullglob
        env_dirs=("$PIXI_ENVS_DIR"/*/)
        shopt -u nullglob
    fi
    for env_dir in ${env_dirs[@]+"${env_dirs[@]}"}; do
        env_dir="${env_dir%/}"
        marker="$env_dir/permissions.done"
        if [[ "$forced" -eq 0 && -e "$marker" ]]; then
            continue
        fi
        echo "==> Setting $ADMIN_GROUP:rwx, others:r-x on $env_dir"
        # mpermissions keeps going past failures on files owned by someone
        # else and always exits 0 regardless, so a clean run is judged by its
        # output (it logs "CMD FAILED" per failed step), not its exit code.
        output="$("$PRODUCTION_REPO/bin/mpermissions" -g "$ADMIN_GROUP" --other-read "$(realpath "$env_dir")" 2>&1)" || true
        echo "$output"
        if [[ "$output" != *"CMD FAILED"* ]]; then
            touch "$marker" 2>/dev/null || echo "WARNING: could not write $marker" >&2
        else
            # A forced re-check that is no longer clean must not leave a stale
            # marker claiming it is.
            rm -f "$marker" 2>/dev/null || true
        fi
    done
}

# Everything pixi creates from here on (new env prefixes, task-cache files) is
# group-writable, so a new env needs no --fix-permissions afterwards. That flag
# is only for what was created before, or by someone with a 022 umask. Set after
# the git pull so the production checkout's own files keep git's modes.
umask 002

# Before the install as well: `pixi install` needs write access to every env
# prefix (install lock, conda-meta), so envs built by another user must be
# made writable for $ADMIN_GROUP first or the install aborts (set -e) and the
# later fix never runs. Only files owned by the invoking user can be changed;
# anything owned by someone else has to be fixed by that user running this
# script with --fix-permissions.
fix_env_permissions

echo "==> Installing all environments"
cd "$MQPIXI_DIR"
pixi install -a --frozen

fix_env_permissions
fix_activation_permissions

echo "==> Running mqpixi postinstall"
cd "$MQPIXI_DIR"
pixi run postinstall

fix_env_permissions
# After postinstall too: it (re)writes pixi_vars.sh in every activate.d.
fix_activation_permissions
