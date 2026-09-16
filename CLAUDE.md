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
  (`AWS_BEARER_TOKEN_BEDROCK`); mqyolo's explicit `--env` list deliberately omits
  `AWS_PROFILE` and the access-key/session-token trio, which carry the caller's
  whole AWS identity. (Note this is not *isolation*: neither script passes
  `--cleanenv`, so anything already exported in the caller's shell reaches the
  container regardless — the `--env` list only controls what mqyolo adds.)
- **Bedrock credentials are staged as a Bedrock-only `~/.aws`, not by exposing the
  real one.** Symptom when this is missing: Claude starts, takes a prompt, and
  never answers — no error even under `--debug` — because `~/.aws` is shadowed, the
  profile from `~/.claude/settings.json`'s `env` block is unresolvable, and the SDK
  falls through to `169.254.169.254`, which this network *blackholes* rather than
  refuses. `_mqyolo_bedrock_profile` detects Bedrock (environment first, then
  settings.json — a plain login shell has none of it set, since Claude applies that
  block to its own process) and `sandbox_stage_bedrock_credentials` writes just that
  profile's static keys plus its region into `${CONTAINER_HOME}/.aws`. The SSO
  token, `~/.aws/cli/cache` and every other profile stay invisible; the profile must
  already hold static keys (what `mqbedrock` maintains), and an SSO-only profile is
  a warning, not a silent hang. `AWS_PROFILE` *is* forwarded once staged — safe
  because only that profile exists there, and needed by tools that don't read
  Claude's settings.json. Skipped entirely when `AWS_BEARER_TOKEN_BEDROCK` is set,
  or when `--ro-paths ~/.aws` already opted the real directory back in.
  `AWS_EC2_METADATA_DISABLED` is set in `sandbox_build_env` so any future credential
  gap errors immediately instead of hanging (an explicit host value wins).
- **Refreshing those keys must happen on the host.** They expire (12h for an
  SSO-issued role) and nothing in the sandbox can renew them, so `mqbedrock` is on
  the broker allowlist (only when a credential was staged), it self-dispatches to
  the broker stub when `MQBROKER_SPOOL` is set — so one `awsAuthRefresh` entry in
  settings.json works on both sides — and the broker restages the credentials after
  a successful run, atomically, so a live session picks them up without relaunch.
  The broker forces `--no-login` and rejects every other argument: an interactive
  `aws sso login` would block it polling for a device code the container can never
  display, since the stub only replays output once the command has finished. Past
  the SSO session's own expiry the re-login is therefore a `mqbedrock` run on the
  host, and `--no-login` says exactly that instead of hanging.
  **The stub must only read stdin for the commands that consume it** (`qsub`, and
  `mqsub --script -`). Claude Code runs `awsAuthRefresh` with a socket on the
  hook's stdin that it never writes to and never closes, so an unconditional
  `cat` blocked before the request was even renamed into the spool: the broker
  never saw it, and the symptom was an empty `Authentication` panel — no device
  code, no error, nothing — until Claude SIGTERMed the hook at its 3-minute
  timeout. Guarded by `test_stub_does_not_block_on_an_idle_stdin_pipe`.
- **`mqbedrock --setup-claude` / `--setup-codex` / `--setup-opencode` write the
  client-side config; the
  refresh path writes credentials.** Keep the two separable: the `--setup-*` options
  make no AWS call (so they work on a machine that has never logged in), are exempt
  from the broker forwarding, and never silently replace a user file — settings are
  compared as JSON (key order and whitespace irrelevant, `jq` or `python3`; the
  opencode config the same way), the
  Codex profile file by its generated-by marker, and `--force` is what overwrites,
  always keeping a `<file>.mqbedrock-<timestamp>.bak`. `--setup-claude` also writes
  `~/.claude.json` with just `hasCompletedOnboarding`: the first-run walkthrough has
  nothing to log into when the credential is an AWS profile, and in a sandbox it is a
  dead end. That file accumulates real state (projects, MCP approvals), which is why
  the backup matters. `--force` without a `--setup-*`, `--codex-*` without
  `--setup-codex` and `--opencode-*` without `--setup-opencode` are errors rather
  than silent no-ops.
- **Codex on Bedrock is a different backend from Claude on Bedrock.** Codex speaks
  only the OpenAI protocol, so it cannot reach Claude there at all; its built-in
  `amazon-bedrock-runtime` provider is Bedrock's OpenAI-compatible endpoint
  (`bedrock-runtime.<region>.amazonaws.com/openai/v1`) serving the OpenAI models,
  and it signs with SigV4 — so the same static `[bedrock]` profile `mqbedrock`
  maintains works for both. `mqbedrock --setup-codex` writes that configuration as
  a Codex *profile file* (`$CODEX_HOME/bedrock.config.toml`, selected by
  `codex --profile bedrock`) rather than into `config.toml`: the file is then
  entirely ours (no mangling a `config.toml` full of `[projects."..."]` trust
  entries), it changes nothing until selected, and — unlike a project-local config,
  which is denylisted from provider/auth keys — a profile layer may set
  `model_provider`/`model_providers`. It is exempt from mqbedrock's
  broker-forwarding (it makes no AWS call and `~/.codex` is bound read-write, so it
  works from either side). mqyolo reads the AWS profile out of that file
  (`_mqyolo_codex_aws_profile`) and stages it — for codex that file is
  authoritative even over `AWS_BEARER_TOKEN_BEDROCK`, because Codex's own auth
  precedence puts a configured `aws.profile` first — then passes
  `--profile bedrock` unless the caller passed their own `--profile`/`-p` or
  `--no-bedrock` (which for codex is exactly "leave the profile unselected"; the
  claude-only settings.json rewrite is unchanged). `aws.auth_refresh` in the file
  is Codex's equivalent of `awsAuthRefresh`, and pointing it at bare `mqbedrock`
  makes the expiry path work on both sides via the existing broker stub.
  Not currently usable at QUT: that endpoint authorizes `bedrock:InvokeModel`
  against `arn:aws:bedrock:<region>:<account>:project/default`, not a model ARN,
  and the `DFAZCB7230-BedrockUserAccess` role has no such grant — every model
  (including `au.anthropic.*`) returns AccessDenied there, while per-model
  `Converse`, which Claude Code uses, succeeds. `--setup-codex` prints that.
- **opencode on Bedrock is the one that does reach Claude there**, because its
  built-in `amazon-bedrock` provider (`@ai-sdk/amazon-bedrock`) calls the per-model
  `Converse`/`ConverseStream` API — the same one Claude Code uses, i.e. the one QUT
  grants — so the `au.anthropic.*` inference profiles work and it signs with the
  same static `[bedrock]` profile. `mqbedrock --setup-opencode` writes
  `~/.config/opencode/bedrock.json` (`--opencode-config NAME` for another name):
  `provider.amazon-bedrock.options.{profile,region}` plus `model`
  (`amazon-bedrock/au.anthropic.claude-sonnet-5` — **Sonnet is the default**) and
  `small_model` (Haiku, for titles and other cheap side-tasks). It is a whole
  config *file* rather than an edit to `opencode.json`, selected with
  `OPENCODE_CONFIG=<file>`, which opencode loads as an **additional layer over**
  the global config (winning only on the keys it sets) — so the file is entirely
  ours, the user's own config still applies, and nothing changes until it is
  selected. Written as plain JSON with no JSONC comments (opencode's loader accepts
  them, and silently drops unknown keys such as the `"//"` note) so that
  `_mqyolo_opencode_aws_profile` can read the profile back out with a JSON parser
  and `write_json` can compare an existing file as JSON — the same
  never-clobber-a-user-file rule as `--setup-claude`, with `--force` + backup.
  mqyolo reads that profile, stages it, and pins `OPENCODE_CONFIG` at the
  in-container path via `--env` **and** `SANDBOX_SHIM_EXPORTS` (a user bashrc would
  otherwise win, as for the XDG vars). Skipped when the caller set
  `OPENCODE_CONFIG` themselves or passed `--no-bedrock` (for opencode that is all
  the flag does). Unlike codex, `AWS_BEARER_TOKEN_BEDROCK` wins over a configured
  profile in opencode's own credential order, so with one set nothing is staged
  while the config is still selected. opencode has **no** auth-refresh hook
  (nothing like `awsAuthRefresh`/`aws.auth_refresh` exists in it), so an expired
  key means running `mqbedrock` again — from inside the sandbox that is the broker
  stub, which restages the credentials live.
- **opencode's `--auto` is declared per command, so mqyolo cannot just prefix it.**
  Both the default interactive command and `run` declare it, other subcommands do
  not, and the parser is strict about unknown options — so `opencode --auto run ...`
  printed opencode's top-level help and ran nothing, and `opencode --auto models`
  would print the `models` help. The tool case therefore chooses the position: no
  args or a leading option/path-ish first argument (the interactive command's
  optional `project` positional) keeps the prefix; a first argument of `run` becomes
  `run --auto <message...>`; any other bare word is assumed to be a subcommand and
  gets no `--auto` (none of them run tools). Inserting it right after `run` rather
  than appending it also keeps it out of `run`'s variadic message — appended after a
  `--` separator it would become part of the prompt instead of a flag. Guarded by
  `test_mqyolo_opencode_run_puts_auto_after_the_subcommand` and friends.
- mqyolo refuses to launch unless the working directory is within `/work/microbiome`,
  `$HOME`, `/scratch/microbiome/$USER`, or `/tmp` (anti-leakage; the CWD is bound
  read-write). Checked before the runtime/image checks.
- Jobs submitted from inside the container are always `--sandbox`ed and inherit
  mqyolo's fixed `--rw-paths`; the container cannot change them (`--no-sandbox` and
  `--sandbox-rw-paths` from the container are rejected).
- NVIDIA GPUs are passed through when the host has a driver: `sandbox_add_gpu_args`
  in `_sandbox_common.bash` sets `GPU_ARGS=(--nv)`, and both mqyolo and mqsandbox
  pass `GPU_ARGS` to `apptainer exec`. `--contain` otherwise hides both halves the host
  must supply — the driver userspace libs (`libcuda.so`, only in the host's
  `/usr/lib64`) and `/dev/nvidia*` — and CUDA then fails with
  `cudaErrorInsufficientDriver`, which misleadingly reads as version skew but is
  really "no driver at all". Conda/pixi ship only the CUDA runtime. Detection is by
  device node and happens where the container is launched, i.e. **on the compute
  node inside the PBS job** for `mqsub --sandbox`, so `--A100`/`--H100` jobs get a
  GPU while CPU-only login nodes are unaffected. **The sandbox is deliberately not
  restricted to mirror the submitting environment**: `sandbox_wrap` in `mqsub`
  bakes no GPU flag into the job script (only `--cwd`/`--rw-paths`/`--ro-paths`),
  so a job submitted from a CPU-only login node still gets full GPU access when it
  lands on a GPU node. `MQSANDBOX_NV=0`/`1` forces it off/on; because mqsub does
  not `qsub -V`, `sandbox_wrap` forwards that variable explicitly as an
  `MQSANDBOX_NV=... mqsandbox ...` prefix so the override survives the submit
  boundary. `SANDBOX_GPU_DEVICE_GLOBS` is used for **detection only** — we do not
  bind the device nodes, because `--nv` already binds them under `--contain`, and a
  superset at that (a verification run on gpu0n008 also got `/dev/nvidia-nvlink`
  and `/dev/nvidia-nvswitch0-5`). Binding them ourselves was tried and reverted: it
  earned a `Skipping /dev/nvidiaN bind mount: already mounted` warning per device on
  every GPU job (13 lines on an 8-GPU node) straight into every snakemake log. If a
  runtime ever stops binding them, the symptom is the `cudaErrorInsufficientDriver`
  signature above plus an empty `ls /dev/nvidia*` inside the sandbox. mqsandbox also
  forwards `CUDA_VISIBLE_DEVICES` (and `NVIDIA_VISIBLE_DEVICES`,
  `CUDA_DEVICE_ORDER`, `GPU_DEVICE_ORDINAL`) explicitly, since PBS sets it to
  select the allocated GPU.
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
  all four dirs opencode uses — `~/.config/opencode`, `~/.local/share/opencode`,
  `~/.local/state/opencode` and `~/.cache/opencode` — so config/auth/sessions/state
  persist. opencode recursively mkdirs all four at startup, so any one left on the
  read-only home is a hard launch failure (`EROFS ... mkdir
  '/container_home/.local/state/opencode'`), not just lost persistence. Their XDG
  parents are mirrored into the ephemeral home as directories of symlinks by
  `sandbox_mirror_home_subdir`, and `XDG_CONFIG_HOME`/`XDG_DATA_HOME`/
  `XDG_STATE_HOME`/`XDG_CACHE_HOME` are all pinned to the container home so host
  values cannot redirect opencode).
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
