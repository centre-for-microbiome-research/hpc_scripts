# hpc_scripts — Claude instructions

## mqyolo sandbox / mqsub broker stack

These files form one coupled system that runs an AI tool inside a restricted
Apptainer container and lets `mqsub` be driven from inside it, with submitted jobs
boxed into the same sandbox:

- `bin/mqyolo` — interactive sandboxed AI session; starts one mqsub broker per session
- `bin/mqsandbox` — runs an arbitrary command inside the restricted container
- `bin/_sandbox_common.bash` — shared bind/env construction (sourced by the two above)
- `bin/mqsub-broker` — host-side broker; runs allowlisted commands, forces `--sandbox`
- `bin/mqbroker-stub` — container-side stub (symlinked as mqsub/mqstat/mqwait/mqdel)
- `bin/mqsub` — `--sandbox` / `--sandbox-rw-paths` wrap the job in `mqsandbox`

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
- Jobs submitted from inside the container are always `--sandbox`ed and inherit
  mqyolo's fixed `--rw-paths`; the container cannot change them (`--no-sandbox` and
  `--sandbox-rw-paths` from the container are rejected).
- The broker is tied to the mqyolo session and self-terminates when the mqyolo PID
  disappears.
