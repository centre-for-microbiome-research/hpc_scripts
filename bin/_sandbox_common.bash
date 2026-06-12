# _sandbox_common.bash - shared Apptainer/Singularity sandbox construction.
#
# Sourced by mqyolo (interactive AI tool sandbox) and mqsandbox (generic
# command runner, used to box in PBS jobs submitted via `mqsub --sandbox`).
# Keeping the bind/env logic in one place means an interactive session and the
# jobs it submits enforce *exactly* the same filesystem constraints:
#   - the working directory:  read-write
#   - /tmp, /var/tmp:         read-write
#   - HPC data mounts, /etc:  read-only
#   - deny-listed paths:      not visible at all (anti-exfiltration), with a
#                             small read-only exception list — see SANDBOX_DENY_*
#   - everything else:        not visible (--contain)
#   - $HOME:                  ephemeral, dotfiles symlinked from the real home
#
# This file is not executable and has no effect on its own. Consumers source it
# and call the functions below, which populate the shared arrays/variables:
#   RUNTIME          apptainer|singularity
#   CONTAINER_HOME   ephemeral home dir (caller is responsible for cleanup trap)
#   BIND_ARGS        array of --bind ... arguments
#   ENV_ARGS         array of --env ... arguments
#   CONTAINER_PATH   value used for PATH inside the container

# ---------------------------------------------------------------------------
# sandbox_detect_runtime
#   Sets RUNTIME to apptainer (preferred) or singularity, or exits 1.
# ---------------------------------------------------------------------------
sandbox_detect_runtime() {
    if command -v apptainer &>/dev/null; then
        RUNTIME=apptainer
    elif command -v singularity &>/dev/null; then
        RUNTIME=singularity
    else
        echo "Error: neither apptainer nor singularity found in PATH" >&2
        exit 1
    fi
}

# ---------------------------------------------------------------------------
# sandbox_require_sif SIF
#   Verifies the container image exists, or exits 1 with build instructions.
# ---------------------------------------------------------------------------
sandbox_require_sif() {
    local sif="$1"
    if [[ ! -f "$sif" ]]; then
        echo "Error: container image not found: $sif" >&2
        echo "Build it with:" >&2
        echo "  apptainer build --fakeroot ai_tool.sif ai_tool.def" >&2
        exit 1
    fi
}

# ---------------------------------------------------------------------------
# sandbox_make_home
#   Creates the ephemeral container home in CONTAINER_HOME and binds it as
#   /container_home (HOME inside the container). The CALLER must arrange for
#   `rm -rf "$CONTAINER_HOME"` on exit (see the trap in mqyolo/mqsandbox).
# ---------------------------------------------------------------------------
sandbox_make_home() {
    CONTAINER_HOME="$(mktemp -d)"
    BIND_ARGS+=(--bind "${CONTAINER_HOME}:/container_home:rw")
}

# ---------------------------------------------------------------------------
# Anti-exfiltration deny-list.
#   Host paths that must NOT be exposed inside the sandbox at all — not even
#   read-only — so a compromised/over-eager AI (or a job it submits) cannot read
#   and exfiltrate them. SANDBOX_RO_ALLOW_PATHS are read-only EXCEPTIONS carved
#   back out of the denied trees because the sandbox legitimately needs them:
#   shared tools (/work/microbiome/sw is even on PATH) and reference databases.
#   Enforcement lives in sandbox_build_binds (the wholesale mount list and the
#   /proc/mounts loop skip denied paths; denied paths nested under a still-exposed
#   parent are shadowed by an empty dir, then the exceptions are re-bound on top).
# ---------------------------------------------------------------------------
SANDBOX_DENY_PATHS=(/scratch /work/microbiome)
SANDBOX_RO_ALLOW_PATHS=(/work/microbiome/sw /work/microbiome/db)

# ---------------------------------------------------------------------------
# sandbox_path_denied PATH
#   True (0) if PATH is inside a SANDBOX_DENY_PATHS prefix and NOT inside a
#   SANDBOX_RO_ALLOW_PATHS exception; false (1) otherwise.
# ---------------------------------------------------------------------------
sandbox_path_denied() {
    local p="$1" d a
    for a in "${SANDBOX_RO_ALLOW_PATHS[@]+"${SANDBOX_RO_ALLOW_PATHS[@]}"}"; do
        [[ "$p" == "$a" || "$p" == "$a"/* ]] && return 1
    done
    for d in "${SANDBOX_DENY_PATHS[@]+"${SANDBOX_DENY_PATHS[@]}"}"; do
        [[ "$p" == "$d" || "$p" == "$d"/* ]] && return 0
    done
    return 1
}

# ---------------------------------------------------------------------------
# sandbox_build_binds CWD [RW_PATH...] [-- RO_PATH...]
#   Populates BIND_ARGS with the read-only system/HPC binds, the anti-exfiltration
#   deny-list (see SANDBOX_DENY_PATHS above), writable temp dirs, any extra
#   read-write paths, any extra read-only paths (those given after a `--`
#   separator), and finally CWD read-write (added LAST so it shadows any
#   read-only parent already bound).
# ---------------------------------------------------------------------------
sandbox_build_binds() {
    local cwd="$1"; shift
    # Remaining args are rw paths, then (after an optional `--`) ro paths.
    local -a rw_paths=() ro_paths=()
    local _seen_sep=0 _arg
    for _arg in "$@"; do
        if [[ "$_seen_sep" -eq 0 && "$_arg" == "--" ]]; then
            _seen_sep=1; continue
        fi
        if [[ "$_seen_sep" -eq 0 ]]; then rw_paths+=("$_arg"); else ro_paths+=("$_arg"); fi
    done

    # --- Read-only system/network config ---
    # Bind only the specific /etc files needed for DNS and TLS — do NOT bind the
    # host's entire /etc over the container's.  In Debian (the node:22 base) many
    # core commands (awk, which, editor, pager, ...) are symlinks that resolve
    # through the alternatives tree, e.g.
    #     /usr/bin/awk -> /etc/alternatives/awk -> /usr/bin/mawk
    # A blanket "--bind /etc:/etc:ro" replaces the container's /etc/alternatives
    # with the host's, whose targets don't exist inside the container, so those
    # symlinks dangle and the commands appear "not found".  Binding individual
    # files leaves the container's /etc/alternatives intact.
    local _etc
    for _etc in /etc/resolv.conf /etc/hosts /etc/nsswitch.conf /etc/localtime \
                /etc/passwd /etc/group /etc/ssl /etc/pki /etc/ca-certificates; do
        [[ -e "$_etc" ]] && BIND_ARGS+=(--bind "${_etc}:${_etc}:ro")
    done
    # /etc/resolv.conf may be a symlink (e.g. systemd-resolved); bind the real
    # target so DNS works inside the container.
    local _resolv_real
    _resolv_real="$(realpath /etc/resolv.conf 2>/dev/null || true)"
    if [[ -n "$_resolv_real" && "$_resolv_real" != /etc/resolv.conf && -f "$_resolv_real" ]]; then
        BIND_ARGS+=(--bind "${_resolv_real}:${_resolv_real}:ro")
    fi

    # Common HPC data mounts — bind read-only so their (possibly non-mountpoint)
    # parent directories are visible inside the container.
    local dir
    for dir in /home /mnt /pkg /external /scratch /nfs /data /storage /projects /work /project; do
        sandbox_path_denied "$dir" && continue
        [[ -d "$dir" ]] && BIND_ARGS+=(--bind "${dir}:${dir}:ro")
    done

    # Bind EVERY real mountpoint from /proc/mounts read-only at its own path.
    #
    # This is essential, not just belt-and-braces: Apptainer's "--bind /mnt:ro"
    # does NOT propagate read-only to nested mounts. On this HPC /mnt/hpccs01 (and
    # similar) are separate lustre filesystems, so a bare "/mnt:ro" leaves the
    # entire home/work tree WRITABLE inside the container. Each filesystem must be
    # bound ro at its own mountpoint for the read-only guarantee to actually hold.
    # Writable paths (/tmp, --rw-paths, CWD) are bound AFTER these and win.
    local SKIP_PATTERN='^/(sys|proc|dev|run|boot|usr|lib|lib64|bin|sbin|opt|etc)(/|$)'
    local _ mountpoint fstype _rest
    while read -r _ mountpoint fstype _rest; do
        case "$fstype" in
            sysfs|proc|devtmpfs|devpts|tmpfs|cgroup*|pstore|efivarfs|bpf|overlay|\
            fuse.lxcfs|fusectl|hugetlbfs|mqueue|debugfs|tracefs|securityfs|\
            configfs|squashfs|ramfs|rpc_pipefs|autofs)
                continue ;;
        esac
        [[ "$mountpoint" == "/" ]] && continue
        # /tmp and /var/tmp (and anything under them) stay writable — bound below.
        [[ "$mountpoint" == /tmp || "$mountpoint" == /tmp/* ]] && continue
        [[ "$mountpoint" == /var/tmp || "$mountpoint" == /var/tmp/* ]] && continue
        [[ "$mountpoint" =~ $SKIP_PATTERN ]] && continue
        # Never expose deny-listed mounts (e.g. a nested /work/microbiome lustre
        # filesystem), even though they are real mountpoints.
        sandbox_path_denied "$mountpoint" && continue
        [[ -d "$mountpoint" ]] && BIND_ARGS+=(--bind "${mountpoint}:${mountpoint}:ro")
    done < /proc/mounts

    # --- Enforce the deny-list for paths nested under a still-exposed parent ---
    # A denied dir nested under an ro-bound parent (e.g. /work/microbiome under the
    # wholesale ro-bound /work) would otherwise leak through that parent bind.
    # Shadow it with an empty dir (with the allowed sub-paths pre-created as
    # mountpoints), so only the explicitly allowed sub-paths bound below remain
    # visible. Top-level denied dirs (e.g. /scratch) are simply never bound above,
    # so under --contain they never appear and need no shadow.
    local _deny _shadow _allow
    for _deny in "${SANDBOX_DENY_PATHS[@]+"${SANDBOX_DENY_PATHS[@]}"}"; do
        [[ "$_deny" == /*/* ]] || continue
        _shadow="${CONTAINER_HOME}/_denied${_deny//\//_}"
        mkdir -p "$_shadow"
        for _allow in "${SANDBOX_RO_ALLOW_PATHS[@]+"${SANDBOX_RO_ALLOW_PATHS[@]}"}"; do
            [[ "$_allow" == "$_deny"/* ]] && mkdir -p "${_shadow}/${_allow#"$_deny"/}"
        done
        BIND_ARGS+=(--bind "${_shadow}:${_deny}:ro")
    done
    # Re-bind the allowed read-only exceptions on top of the shadows.
    for _allow in "${SANDBOX_RO_ALLOW_PATHS[@]+"${SANDBOX_RO_ALLOW_PATHS[@]}"}"; do
        [[ -d "$_allow" ]] && BIND_ARGS+=(--bind "${_allow}:${_allow}:ro")
    done

    # --- Extra caller-specified read-only paths (e.g. mqyolo --ro-paths) ---
    # Bound AFTER the deny-list enforcement, so an explicit --ro-path can re-expose
    # a specific subdirectory of an otherwise-denied tree if the user opts in.
    local _ro_path _ro_canonical
    for _ro_path in "${ro_paths[@]+"${ro_paths[@]}"}"; do
        _ro_canonical="$(realpath "$_ro_path" 2>/dev/null || echo "$_ro_path")"
        [[ -e "$_ro_canonical" ]] && BIND_ARGS+=(--bind "${_ro_canonical}:${_ro_canonical}:ro")
    done

    # --- Writable temp dirs ---
    [[ -d /tmp ]]     && BIND_ARGS+=(--bind "/tmp:/tmp:rw")
    [[ -d /var/tmp ]] && BIND_ARGS+=(--bind "/var/tmp:/var/tmp:rw")

    # --- Extra caller-specified rw paths ---
    local _rw_path _rw_canonical
    for _rw_path in "${rw_paths[@]+"${rw_paths[@]}"}"; do
        _rw_canonical="$(realpath "$_rw_path" 2>/dev/null || echo "$_rw_path")"
        [[ -e "$_rw_canonical" ]] && BIND_ARGS+=(--bind "${_rw_canonical}:${_rw_canonical}:rw")
    done

    # --- Writable current directory (added LAST to shadow any parent ro bind) ---
    BIND_ARGS+=(--bind "${cwd}:${cwd}:rw")

    # --- Cargo build cache (share target/ with the container) ---
    if [[ -f "${cwd}/Cargo.toml" && -d "${cwd}/target" ]]; then
        BIND_ARGS+=(--bind "${cwd}/target:${cwd}/target:rw")
    fi

    # --- Writable /pkg/cmr/{username}/pixi_dirs ---
    local _pixi_dirs="/pkg/cmr/${USER}/pixi_dirs"
    [[ -d "$_pixi_dirs" ]] && BIND_ARGS+=(--bind "${_pixi_dirs}:${_pixi_dirs}:rw")

    # --- Writable pixi/rattler cache dir ---
    # PIXI_CACHE_DIR / RATTLER_CACHE_DIR commonly point into shared storage on
    # /pkg/cmr or /mnt/weka (see mqlint), which is bound READ-ONLY above, so pixi
    # can't populate its cache. Bind the cache rw at its canonical path so
    # `pixi install` / env regeneration works. The env var is forwarded as the
    # original logical path (sandbox_build_env); inside the container it resolves
    # through the ro-bound parent symlink onto this rw bind.
    local _pixi_cache _pixi_cache_real
    for _pixi_cache in "${PIXI_CACHE_DIR:-}" "${RATTLER_CACHE_DIR:-}"; do
        [[ -n "$_pixi_cache" ]] || continue
        _pixi_cache_real="$(realpath "$_pixi_cache" 2>/dev/null || echo "$_pixi_cache")"
        mkdir -p "$_pixi_cache_real" 2>/dev/null || true
        [[ -d "$_pixi_cache_real" ]] && BIND_ARGS+=(--bind "${_pixi_cache_real}:${_pixi_cache_real}:rw")
    done
}

# ---------------------------------------------------------------------------
# sandbox_home_dotfiles
#   Populates the ephemeral home (CONTAINER_HOME must already be set) with:
#     - an empty dir shadowing ~/.ssh (so keys are not readable)
#     - rw binds of ~/.cargo (resolving symlinks into shared storage)
#     - rw binds of each ~/.cache entry (resolving symlinks)
#     - symlinks to every other ~/dotfile so tools pick up the user's config
#   Skips .claude / .claude.json (mqyolo handles those) and the entries above.
# ---------------------------------------------------------------------------
sandbox_home_dotfiles() {
    # --- Hide ~/.ssh completely (shadow with an empty dir) ---
    if [[ -d "${HOME}/.ssh" ]]; then
        local _ssh_real
        _ssh_real="$(realpath "${HOME}/.ssh" 2>/dev/null || echo "${HOME}/.ssh")"
        mkdir -p "${CONTAINER_HOME}/_empty_ssh"
        BIND_ARGS+=(--bind "${CONTAINER_HOME}/_empty_ssh:${_ssh_real}:ro")
    fi

    # Bind CARGO_HOME (default ~/.cargo) rw so `cargo build`/`cargo test` can
    # download missing deps into the registry.
    #
    # On HPC ~/.cargo is commonly a symlink into shared storage, e.g.
    #     ~/.cargo -> /pkg/cmr/$USER/.cargo   (realpath /mnt/weka/pkg/...)
    # Inside the container /home, /pkg and /mnt are all bound READ-ONLY, so an rw
    # bind onto the *logical* path lands on the symlink and writes fall through to
    # the ro mount. Bind the real cargo dir rw at every path the container may
    # resolve it through — its realpath, and (if ~/.cargo is a symlink) the
    # symlink's literal target. CARGO_HOME itself stays the original logical path
    # (set in sandbox_build_env) so the registry-src paths recorded in target/
    # don't change and cargo isn't forced into a full rebuild.
    local _cargo_home _cargo_home_real _cargo_link
    _cargo_home="${CARGO_HOME:-${HOME}/.cargo}"
    _cargo_home_real="$(realpath "$_cargo_home" 2>/dev/null || true)"
    if [[ -n "$_cargo_home_real" && -d "$_cargo_home_real" ]]; then
        BIND_ARGS+=(--bind "${_cargo_home_real}:${_cargo_home_real}:rw")
        _cargo_link="$(readlink "$_cargo_home" 2>/dev/null || true)"
        if [[ -n "$_cargo_link" && "$_cargo_link" == /* && "$_cargo_link" != "$_cargo_home_real" ]]; then
            BIND_ARGS+=(--bind "${_cargo_home_real}:${_cargo_link}:rw")
        fi
    fi

    # Bind ~/.cache entries individually with canonical paths so that symlinked
    # subdirs (e.g. rattler -> /pkg/...) get their own rw bind and aren't left
    # pointing at the ro-bound parent filesystem.
    mkdir -p "${CONTAINER_HOME}/.cache"
    local _cache_item _cache_name _cache_target
    while IFS= read -r -d '' _cache_item; do
        _cache_name="${_cache_item##*/}"
        _cache_target="$(readlink -f "$_cache_item" 2>/dev/null || echo "$_cache_item")"
        [[ -d "$_cache_target" ]] || continue
        mkdir -p "${CONTAINER_HOME}/.cache/${_cache_name}"
        BIND_ARGS+=(--bind "${_cache_target}:/container_home/.cache/${_cache_name}:rw")
    done < <(find "${HOME}/.cache" -maxdepth 1 -mindepth 1 -print0 2>/dev/null)

    # Populate /container_home with symlinks to the real home so that ~ inside the
    # container resolves to the user's actual files.  .claude and .claude.json are
    # skipped because mqyolo handles them with writable bind mounts; .cache and
    # .ssh are handled above.
    local item name
    while IFS= read -r -d '' item; do
        name="${item##*/}"
        [[ "$name" == ".claude" || "$name" == ".claude.json" || "$name" == ".cache" || "$name" == ".ssh" ]] && continue
        # Resolve through any intermediate symlinks (e.g. /pkg -> /mnt/weka/pkg)
        # so the target is a canonical /mnt/... path that is bound in.
        ln -sf "$(readlink -f "$item" 2>/dev/null || echo "$item")" "${CONTAINER_HOME}/${name}" 2>/dev/null || true
    done < <(find "${HOME}" -maxdepth 1 -mindepth 1 -print0 2>/dev/null)
}

# ---------------------------------------------------------------------------
# Repo helper scripts that should be on PATH inside the sandbox. These ship with
# this repo (the same tree mqyolo runs from) so the in-container AI always has them
# and runs the version that ships with mqyolo, independent of any separately
# deployed copy under /work/microbiome/sw.
# ---------------------------------------------------------------------------
SANDBOX_REPO_TOOLS=(pixi_cmr_init.py)

# ---------------------------------------------------------------------------
# sandbox_stage_repo_tools TOOLS_DIR SCRIPT_DIR
#   Symlink each SANDBOX_REPO_TOOLS entry found in SCRIPT_DIR into TOOLS_DIR,
#   pointing at the canonical target (bound ro under /home|/mnt|/work) so it
#   resolves inside the container. mqyolo creates TOOLS_DIR under its ephemeral
#   home and adds it to the container PATH (SANDBOX_PATH_PREFIX), so the shim
#   ~/.bashrc keeps it ahead of the user's bashrc-prepended hpc_scripts bin dir.
# ---------------------------------------------------------------------------
sandbox_stage_repo_tools() {
    local tools_dir="$1" script_dir="$2" _t
    mkdir -p "$tools_dir"
    for _t in "${SANDBOX_REPO_TOOLS[@]}"; do
        [[ -e "${script_dir}/${_t}" ]] && \
            ln -sf "$(readlink -f "${script_dir}/${_t}")" "${tools_dir}/${_t}"
    done
}

# ---------------------------------------------------------------------------
# sandbox_bind_codex_home REAL_CODEX DEST_CODEX [GUIDANCE_FILE]
#   Bind the real Codex home read-write into the container so Codex config,
#   sessions and auth changes persist. When guidance is provided, bind that file
#   over DEST_CODEX/AGENTS.md inside the container without editing the real file.
# ---------------------------------------------------------------------------
sandbox_bind_codex_home() {
    local real="$1" dest="$2" guidance="${3:-}"

    [[ -n "$real" ]] || return 0
    mkdir -p "$real"
    BIND_ARGS+=(--bind "${real}:${dest}:rw")

    if [[ -n "$guidance" && -f "$guidance" ]]; then
        BIND_ARGS+=(--bind "${guidance}:${dest}/AGENTS.md:ro")
    fi
}

# ---------------------------------------------------------------------------
# sandbox_write_shim_bashrc DEST_BASHRC REAL_BASHRC SHIM_DIR
#   Write DEST_BASHRC so a shell that sources it first runs the user's real
#   bashrc (if given/existing) and THEN puts SHIM_DIR first on PATH. This is how
#   the container-side mqsub stub keeps winning over the real hpc_scripts bin
#   dir, which the user's ~/.bashrc (and Claude Code's shell snapshot) prepend.
#   DEST is removed first because it is usually a symlink into the real home —
#   we must NOT write through it onto the user's actual bashrc.
# ---------------------------------------------------------------------------
sandbox_write_shim_bashrc() {
    local dest="$1" real="$2" shim="$3"
    rm -f "$dest"
    {
        [[ -n "$real" && -f "$real" ]] && printf 'source %q\n' "$real"
        printf 'export PATH=%q:"$PATH"\n' "$shim"
    } > "$dest"
}

# ---------------------------------------------------------------------------
# sandbox_build_env [PASSTHROUGH_VAR...]
#   Populates ENV_ARGS and CONTAINER_PATH. Always sets PATH, NODE_EXTRA_CA_CERTS
#   (if a CA bundle exists) and CARGO_HOME (if present). Any PASSTHROUGH_VAR
#   names that are set in the environment are forwarded with --env.
#   If SANDBOX_PATH_PREFIX is set, it is prepended to PATH (used for the broker
#   shim dir so the container-side mqsub stub shadows the real binary).
# ---------------------------------------------------------------------------
sandbox_build_env() {
    local var
    for var in "$@"; do
        [[ -n "${!var:-}" ]] && ENV_ARGS+=(--env "${var}=${!var}")
    done
    # If a system CA bundle exists, tell Node.js to trust it — needed when a TLS
    # inspection proxy (e.g. IAClient) is in use and re-signs outbound HTTPS.
    [[ -f /etc/ssl/certs/ca-certificates.crt ]] && \
        ENV_ARGS+=(--env "NODE_EXTRA_CA_CERTS=/etc/ssl/certs/ca-certificates.crt")
    # Point Cargo at the rw-bound logical host path (same inside and outside)
    local _cargo_home="${CARGO_HOME:-${HOME}/.cargo}"
    [[ -d "$_cargo_home" ]] && ENV_ARGS+=(--env "CARGO_HOME=${_cargo_home}")

    # Build PATH with the container-installed binaries first so host wrappers in
    # ~/bin or ~/.local/bin do not shadow them.
    CONTAINER_PATH="/usr/local/bin:/usr/bin:/bin"
    [[ -d "${HOME}/.local/bin" ]] && CONTAINER_PATH="${CONTAINER_PATH}:${HOME}/.local/bin"
    [[ -d "${HOME}/bin" ]]        && CONTAINER_PATH="${CONTAINER_PATH}:${HOME}/bin"
    [[ -d "/work/microbiome/sw/hpc_scripts/bin" ]] && CONTAINER_PATH="${CONTAINER_PATH}:/work/microbiome/sw/hpc_scripts/bin"
    # Prepend the broker shim dir (if any) so the container-side mqsub stub wins.
    [[ -n "${SANDBOX_PATH_PREFIX:-}" ]] && CONTAINER_PATH="${SANDBOX_PATH_PREFIX}:${CONTAINER_PATH}"
    ENV_ARGS+=(--env "PATH=${CONTAINER_PATH}")
}
