# _sandbox_common.bash - shared Apptainer/Singularity sandbox construction.
#
# Sourced by mqyolo (interactive AI tool sandbox) and mqsandbox (generic
# command runner, used to box in PBS jobs submitted via `mqsub --sandbox`).
# Keeping the bind/env logic in one place means an interactive session and the
# jobs it submits enforce *exactly* the same filesystem constraints:
#   - the working directory:  read-write
#   - /tmp, /var/tmp:         read-write
#   - HPC data mounts, /etc:  read-only
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
# sandbox_build_binds CWD [RW_PATH...]
#   Populates BIND_ARGS with the read-only system/HPC binds, writable temp
#   dirs, any extra read-write paths, and finally CWD read-write (added LAST so
#   it shadows any read-only parent already bound).
# ---------------------------------------------------------------------------
sandbox_build_binds() {
    local cwd="$1"; shift
    local -a rw_paths=("$@")

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
        [[ -d "$mountpoint" ]] && BIND_ARGS+=(--bind "${mountpoint}:${mountpoint}:ro")
    done < /proc/mounts

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
