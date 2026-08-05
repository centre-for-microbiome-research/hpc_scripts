# hpc_scripts — Claude instructions

## mqyolo sandbox / mqsub broker stack

These files form one coupled system that runs an AI tool inside a restricted
Apptainer container and lets `mqsub` be driven from inside it, with submitted jobs
boxed into the same sandbox:

- `bin/mqyolo` — interactive sandboxed AI session; starts one mqsub broker per session
- `bin/mqsandbox` — runs an arbitrary command inside the restricted container
- `bin/_sandbox_common.bash` — shared bind/env construction (sourced by the two above)
- `bin/mqsub-broker` — host-side broker; runs allowlisted commands, forces `--sandbox`
- `bin/mqbroker-stub` — container-side stub (symlinked as mqsub/mqstat/mqwait/mqdel/qstat/qdel)
- `bin/mqsub` — `--sandbox` / `--sandbox-rw-paths` wrap the job in `mqsandbox`
- `bin/generate_mqyolo_non_sensitive_folders.py` — downloads the CMR work-folders
  Google Sheet and writes `mqyolo-non-sensitive-folders.json` (run via the
  `update-non-sensitive-folders` pixi task)
- `mqyolo-non-sensitive-folders.json` — repo-root list of `/work` folders flagged
  "not sensitive" in the sheet; mqyolo reads it via a path relative to the script
  on every launch and auto-mounts each existing folder read-only (appended to
  `RO_PATHS`, so broker-submitted jobs inherit them too). It must ship with the
  repo for the relative-path lookup to resolve in deployed copies.

**Whenever you change any of the files above, run the test suite and make sure it
passes before considering the change done:**

```
pixi run -e dev pytest tests/test_mqyolo_sandbox.py -v
```

`tests/test_mqyolo_sandbox.py` is **local-only** — it requires the HPC environment
(python3.9, apptainer + `singularity/ai_tool.sif`, inotifywait, the lustre mounts)
and self-skips when `GITHUB_ACTIONS`/`CI` is set, so it does **not** run on GitHub
Actions. The broker/wrapping logic tests run on any login node; the two
`mqsandbox`-enforcement tests skip automatically if apptainer or the SIF is
missing. Do not add this test file to `.github/workflows/test.yml`.

Key invariants the tests guard (keep them true):
- Only the working directory plus mqyolo's `--rw-paths` are writable in the sandbox;
  everything else (including nested lustre mounts like `/mnt/hpccs01`) is read-only.
- Remote sshfs (and similar remote FUSE) mounts are denied by default — not visible
  at all, even read-only — so that running mqyolo on a workstation that sshfs-mounts
  sensitive remote trees (e.g. `/work/projects`) does not expose them. They are
  discovered from `/proc/mounts` (`sandbox_collect_remote_deny_mounts` →
  `SANDBOX_DENY_MOUNTS`) and treated like the static deny-list; `--ro-paths`/
  `--rw-paths` still opt a specific path back in. This also covers symlinked
  top-level dirs whose target resolves into a denied mount: the wholesale bind loop
  (`SANDBOX_WHOLESALE_BIND_DIRS`) skips a dir if its realpath is denied — e.g. on a
  workstation where `/work` is a symlink onto the sshfs mount, `/work` is not bound.
- Credential directories in the real home are shadowed with an empty dir bound over
  their realpath, so they are not readable through the read-only home bind
  (`sandbox_home_shadow_dir`): `~/.ssh` unconditionally, and `~/.aws` unless the
  caller passed it via `--ro-paths`/`--rw-paths` (those granted paths are forwarded
  into `sandbox_home_dotfiles` for exactly this check — the shadow binds are appended
  after the caller's binds and `sandbox_dedupe_binds` keeps the last per destination,
  so a shadow would otherwise silently override an explicit opt-in). When `~/.aws` is
  opted in its home symlink must be recreated, or the AWS SDKs cannot find the profile.
  Bedrock access should instead use a credential scoped to model invocation
  (`AWS_BEARER_TOKEN_BEDROCK`); mqyolo deliberately does not forward `AWS_PROFILE` or
  the access-key/session-token trio, which carry the caller's whole AWS identity.
- mqyolo refuses to launch unless the working directory is within `/work/microbiome`,
  `$HOME`, `/scratch/microbiome/$USER`, or `/tmp` (anti-leakage; the CWD is bound
  read-write). Checked before the runtime/image checks.
- Jobs submitted from inside the container are always `--sandbox`ed and inherit
  mqyolo's fixed `--rw-paths`; the container cannot change them (`--no-sandbox` and
  `--sandbox-rw-paths` from the container are rejected).
- Both mqyolo and mqsandbox auto-mount the per-user scratch defaults when present
  (`sandbox_add_default_scratch_paths` in `_sandbox_common.bash`):
  `/scratch/microbiome/$USER/non_sensitive` read-only and its `scratch` and `tmp`
  subdirs (`SANDBOX_SCRATCH_RW_SUBDIRS`) read-write. mqyolo adds them to
  `RO_PATHS`/`RW_PATHS` so the broker forwards them
  to jobs; mqsandbox also adds them itself (deduped) so direct/standalone sandbox
  runs get them too. The AI guidance describes them via `_mqyolo_scratch_guidance`
  (only when the tree exists).
- `snakemake --profile aqua` works inside the container: its cluster helpers
  (`snakemake_mqsub`, `snakemake_mqstat`) are staged onto PATH as repo tools, and
  the `qstat`/`qdel` they (and snakemake's cluster-cancel) rely on are proxied to
  the host via the broker alongside mqsub/mqstat/mqwait/mqdel.
- The broker is tied to the mqyolo session and self-terminates when the mqyolo PID
  disappears.
- The broker only starts when the host actually has the PBS batch queue, i.e.
  `qsub` is on PATH (`_mqyolo_broker_available`). Run off aqua (no `qsub` — e.g. a
  workstation that only sshfs-mounts aqua) the broker is skipped entirely, so the
  container never gets non-working mqsub/qstat stubs, and the AI is instead given
  the "no queue, run locally" guidance (see below). This gate also applies to the
  broker-start condition, not just the guidance.
- The in-container AI tool is told where heavy/long/high-RAM commands should run —
  injected for Claude with `--append-system-prompt-file`, for Codex via its
  global `~/.codex/AGENTS.md` via a read-only file bind over the real read-write
  `~/.codex` mount, and for opencode via the same trick on its global
  `~/.config/opencode/AGENTS.md` (`sandbox_bind_opencode_home`, which also rw-binds
  `~/.config/opencode` and `~/.local/share/opencode` so config/auth/sessions
  persist; their XDG parents are mirrored into the ephemeral home as directories
  of symlinks by `sandbox_mirror_home_subdir`, and `XDG_CONFIG_HOME`/`XDG_DATA_HOME`
  are pinned to the container home so host values cannot redirect opencode).
  The guidance adapts to the boot environment
  (`_mqyolo_detect_resources` → `MQYOLO_ENV` = `login`|`pbs`|`local`): on a login
  node it says offload to `mqsub`; inside a PBS job it reports the actual allocated
  CPUs/RAM (from NCPUS + qstat) and frames them as a finite budget — run work that
  fits directly, but still send larger jobs to the queue via `mqsub` /
  `snakemake --profile aqua`; off the batch queue (`local`) it reports the host's
  own CPUs/RAM and tells the AI there is no queue and to run everything directly
  (never mention `mqsub`). Injected whenever the broker is running (login/pbs) OR
  the queue is unreachable (local); `mqyolo --print-guidance` dumps the exact text
  for the current environment.
