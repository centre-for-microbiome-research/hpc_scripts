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
- mqyolo refuses to launch unless the working directory is within `/work/microbiome`,
  `$HOME`, `/scratch/microbiome/$USER`, or `/tmp` (anti-leakage; the CWD is bound
  read-write). Checked before the runtime/image checks.
- Jobs submitted from inside the container are always `--sandbox`ed and inherit
  mqyolo's fixed `--rw-paths`; the container cannot change them (`--no-sandbox` and
  `--sandbox-rw-paths` from the container are rejected).
- `snakemake --profile aqua` works inside the container: its cluster helpers
  (`snakemake_mqsub`, `snakemake_mqstat`) are staged onto PATH as repo tools, and
  the `qstat`/`qdel` they (and snakemake's cluster-cancel) rely on are proxied to
  the host via the broker alongside mqsub/mqstat/mqwait/mqdel.
- The broker is tied to the mqyolo session and self-terminates when the mqyolo PID
  disappears.
- The in-container AI tool is told where heavy/long/high-RAM commands should run —
  injected for Claude with `--append-system-prompt-file` and for Codex via its
  global `~/.codex/AGENTS.md` via a read-only file bind over the real read-write
  `~/.codex` mount. Only when the broker is running. The guidance adapts to the boot environment
  (`_mqyolo_detect_resources`): on a login node it says offload to `mqsub`; inside
  a PBS job it reports the actual allocated CPUs/RAM (from NCPUS + qstat) and frames
  them as a finite budget — run work that fits directly, but still send larger jobs
  to the queue via `mqsub` / `snakemake --profile aqua`.
  `mqyolo --print-guidance` dumps the exact text for the current environment.
