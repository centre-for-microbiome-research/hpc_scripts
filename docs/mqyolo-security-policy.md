# mqyolo Security Policy

Status: living document. Describes the **intended** security design of the
`mqyolo` sandbox / `mqsub` broker stack. If the code and this document disagree,
that is a bug in one of them — fix it and update the test suite
(`tests/test_mqyolo_sandbox.py`), which encodes most of these invariants.

Scope: the coupled system listed in `CLAUDE.md` —
`bin/mqyolo`, `bin/mqsandbox`, `bin/_sandbox_common.bash`, `bin/mqsub-broker`,
`bin/mqbroker-stub`, and the `--sandbox` path of `bin/mqsub`.

---

## 1. Purpose

`mqyolo` runs an autonomous AI coding tool (Claude Code, Codex, or Copilot) on
the QUT *aqua* HPC with all of its permission prompts disabled
(`--dangerously-skip-permissions` / `--dangerously-bypass-approvals-and-sandbox`).
Because the tool can run arbitrary commands without asking, the **container is
the sandbox**: it, rather than a human approving each action, is what bounds the
blast radius of the AI and of any prompt-injection or model misbehaviour.

The same container is reused for batch jobs the AI submits (`mqsub --sandbox`
via the broker), so an interactive session and the jobs it spawns are confined
to *identical* constraints.

---

## 2. Threat model

We treat the in-container AI tool as **untrusted and potentially adversarial**.
This is not a statement about any particular model; it is the only safe
assumption given that:

- the tool executes commands autonomously with no human in the loop;
- its behaviour can be steered by **prompt injection** from any content it reads
  (repository files, web pages, tool output, datasets);
- coding agents make mistakes that can destroy or leak data.

Adversaries / failure modes in scope:

1. **Data exfiltration** — the AI (or an injected instruction) tries to read
   sensitive data and send it off-node.
2. **Unwanted writes / destruction** — the AI deletes or corrupts files outside
   the task's working area.
3. **Credential theft** — the AI reads SSH keys, tokens, or other secrets to
   move laterally or persist.
4. **Sandbox widening** — the AI tries to grant itself (or its batch jobs) more
   filesystem access than the operator granted at launch.
5. **Host compromise / persistence** — the AI tries to run code on the host
   outside the container, or to leave a process running after the session ends.

Explicitly **out of scope** (see §8): network-level egress controls, container
escape via Apptainer/kernel 0-days, and anything Unix permissions already deny
to the launching user.

---

## 3. Trust boundaries

```
  ┌─────────────────────────── HOST (user's namespace) ───────────────────────────┐
  │                                                                                │
  │   mqyolo (bash)  ── starts ──>  mqsub-broker (bash, allowlisted, host-side)    │
  │      │                                  ▲      │                               │
  │      │ apptainer exec --contain         │ spool (bind-mounted dir)             │
  │      ▼                                  │      ▼                               │
  │  ┌────────────── CONTAINER (--contain, ephemeral) ───────────────┐             │
  │  │  UNTRUSTED AI tool                                            │             │
  │  │   - mqsub/qstat/... are stubs (mqbroker-stub) that drop       │             │
  │  │     request files into the spool; they cannot reach the host │             │
  │  │     directly (Apptainer cannot spawn host processes).        │             │
  │  └──────────────────────────────────────────────────────────────┘             │
  └────────────────────────────────────────────────────────────────────────────────┘
```

The **only** channel from container to host is the spool directory watched by
the broker. The broker is the trust gate: it decides what host commands run and
forces the sandbox policy onto them.

Everything runs as the **launching user**. The sandbox never grants access the
user does not already have; it *removes* access the user has, to contain the
agent.

---

## 4. Containerisation design

### 4.1 Runtime and isolation
- Runs under **Apptainer** (preferred) or Singularity, unprivileged, via
  `exec --contain`. `--contain` gives a clean filesystem namespace: nothing from
  the host is visible unless it is explicitly bind-mounted.
- Image: `singularity/ai_tool.sif` (override with `AI_TOOL_SIF`).

### 4.2 Ephemeral, isolated HOME
- `HOME` inside the container is a fresh `mktemp -d` (`/container_home`), removed
  on exit by a trap (covering normal exit, `INT`/`TERM`/`HUP`, and — via the
  broker's PID watch — even `kill -9`).
- The real `$HOME` is **not** mounted. Selected dotfiles are *symlinked* in so
  tools (git, etc.) find their config, with explicit exceptions below.

### 4.3 Filesystem policy (built in `_sandbox_common.bash`)

| Class | Access | Notes |
|------|--------|-------|
| Working directory (CWD) | **read-write** | Bound last so it wins over any read-only parent. The agent's writable workspace. |
| `/tmp`, `/var/tmp` | read-write | Scratch space. |
| `--rw-paths` (operator-granted) | read-write | Extra writable paths chosen by the human at launch. |
| Build/dep caches | read-write | `~/.cache/*`, `~/.cargo`, pixi/rattler cache, `/pkg/cmr/$USER/pixi_dirs`, CWD `target/` — resolved to canonical paths so writes don't fall through onto a read-only parent. |
| `~/.claude` (claude), `~/.codex` (codex) | read-write | So the tool persists its own config/auth/history across sessions. |
| HPC data mounts (`/home`, `/mnt`, `/work`, `/data`, …), `/etc` selected files | **read-only** | The agent can read reference data and configs but not modify them. |
| `--ro-paths` (operator-granted) + non-sensitive folders (§4.6) | read-only | Extra readable paths chosen at launch / from the sheet. |
| **Deny-listed trees** (§4.4) | **not visible at all** | Anti-exfiltration. |
| Everything else | not visible | `--contain` default. |

### 4.4 Read-only enforcement for nested mounts (critical)
Apptainer's `--bind /mnt:ro` does **not** make nested mountpoints read-only. On
this HPC `/mnt/hpccs01` (home/work) and similar are *separate* Lustre
filesystems, so a bare `/mnt:ro` would leave the entire home/work tree
**writable** inside the container. To prevent this, every real mountpoint in
`/proc/mounts` is bound **read-only at its own path**. This is a guarded
invariant: "nested lustre mounts like `/mnt/hpccs01` are read-only."

### 4.5 Anti-exfiltration deny-list
Some trees are so sensitive that read-only is not enough — they must be
invisible:

- `SANDBOX_DENY_PATHS` = `/scratch`, `/work/microbiome` (project data that may
  contain IP-related or patient data).
- Top-level denied dirs are simply never bound (under `--contain` they never
  appear). A denied dir nested under an otherwise-exposed parent is **shadowed**
  by an empty directory bound over it, so it cannot leak through the parent bind.
- **Alias coverage:** on this HPC the logical paths are symlinks onto canonical
  ones (`/scratch → /mnt/weka/scratch`, `/work/microbiome →
  /mnt/hpccs01/work/microbiome`) and the *same data* is reachable through both.
  The deny logic adds each entry's `realpath` so both the logical and canonical
  aliases are denied — closing the bypass where the canonical path leaks through
  the wholesale `/mnt:ro` bind or the `/proc/mounts` loop.

### 4.6 Read-only exceptions ("carve-outs")
Denied trees have a small, explicit allow-list re-exposed **read-only**:

- `SANDBOX_RO_ALLOW_PATHS` = `/work/microbiome/sw` (shared tools, also on PATH)
  and `/work/microbiome/db` (reference databases).
- **Non-sensitive project folders**: `mqyolo` reads
  `mqyolo-non-sensitive-folders.json` (generated from a human-curated sheet by
  `bin/generate_mqyolo_non_sensitive_folders.py`) on every launch and re-exposes
  each listed folder **read-only**. Only folders explicitly marked "not
  sensitive" are included; sensitive or unmarked folders stay hidden by default
  (fail-closed). These propagate to broker-submitted jobs like `--ro-paths`.

### 4.7 Launch-directory restriction
Because the working directory is bound **read-write**, launching `mqyolo` from an
arbitrary location would needlessly expose (and make writable) unrelated data.
`mqyolo` therefore refuses to start unless the resolved working directory is at
or under one of:

- `/work/microbiome` (project data area),
- the user's `$HOME`,
- `/scratch/microbiome/$USER`,
- `/tmp`.

The check resolves symlinks on both sides (so canonical aliases like
`/mnt/hpccs01/work/microbiome` and `/mnt/weka/scratch/...` are covered) and runs
*before* the container image checks, so a bad launch fails fast with a clear
message. This is a guard against accidental leakage, not a hard security
boundary — it narrows, but does not eliminate, what the read-write working set
can contain (see §8).

### 4.8 Credential and identity hardening
- **SSH keys**: `~/.ssh` is shadowed with an empty read-only directory, so keys
  cannot be read for lateral movement.
- **API keys**: forwarded into the *interactive* container only as needed
  (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GITHUB_TOKEN`). They are **not**
  forwarded to batch jobs (`mqsandbox` passes only `PIXI_CACHE_DIR`,
  `RATTLER_CACHE_DIR`, `TERM`, `COLORTERM`, `NO_COLOR`), so a submitted job
  cannot exfiltrate the session's credentials.
- **passwd/group**: rather than binding the host's files (the LDAP/SSSD user has
  no local entry under `--contain`, which breaks `whoami`/`getpwuid` and tools
  like `mqwait`), a merged file containing just the caller's entry is generated
  and bound read-only — and **re-bound read-only at its `/container_home`
  location** so the writable home bind cannot mutate it through the same inode.

---

## 5. The mqsub broker (container → host bridge)

An Apptainer container cannot spawn processes on the host, and `qsub` / the
`python3.9` that `mqsub` needs do not exist inside it. So the AI cannot submit
jobs directly. Instead:

- `mqyolo` starts **one broker per session** on the host.
- Inside the container, `mqsub`/`mqstat`/`mqwait`/`mqdel`/`qstat`/`qdel` are thin
  stubs (`mqbroker-stub`) that serialise the command, argv, CWD and stdin into a
  request file in a bind-mounted spool. The broker runs the real command on the
  host and writes back stdout/stderr/exit.

Security properties of the broker:

1. **Command allowlist** — only `mqsub mqstat mqwait mqdel qstat qdel` are ever
   run. Any other command name is rejected (exit 126). The container cannot use
   the broker as a generic "run on host" primitive.
2. **Forced sandboxing of jobs** — for `mqsub`, the broker injects `--sandbox`
   so every submitted job runs inside the same restricted container (via
   `mqsandbox`) on its compute node.
3. **Fixed, non-overridable paths** — the broker injects this session's
   `--rw-paths`/`--ro-paths` (set by the human at `mqyolo` launch) onto every
   job, and **rejects** `--no-sandbox`, `--sandbox-rw-paths`, and
   `--sandbox-ro-paths` coming from the container (exit 126). The AI cannot widen
   what its jobs may write to, disable the sandbox, or re-expose denied data.
4. **Session-bound lifetime** — the broker watches `mqyolo`'s PID and exits when
   it disappears (normal exit or `kill -9`), so it is never orphaned and cannot
   become a persistent host-side foothold. `--no-broker` disables it entirely.
5. **Least privilege of `qdel`** — PBS itself ensures a user can only cancel
   their own jobs.

---

## 6. Risks the design is intended to avoid

| # | Risk | Mitigation |
|---|------|-----------|
| 1 | Exfiltration of sensitive project data | Deny-list makes `/scratch` and `/work/microbiome` invisible (incl. canonical aliases); only explicitly non-sensitive carve-outs are readable. |
| 2 | Read-write leak of home/work via non-propagating `--bind :ro` | Every `/proc/mounts` mountpoint bound read-only at its own path. |
| 3 | Alias bypass (canonical vs logical path) | Deny-list expanded with `realpath` of each entry. |
| 4 | Destruction of data outside the task | Only CWD, `/tmp`, caches, and operator `--rw-paths` are writable; all other mounts read-only. |
| 5 | SSH key / credential theft, lateral movement | `~/.ssh` shadowed empty; API keys not forwarded to jobs. |
| 6 | AI widening its own (or its jobs') sandbox | rw/ro paths fixed at launch; broker rejects `--no-sandbox` / `--sandbox-*-paths`; jobs forced through `mqsandbox`. |
| 7 | Running arbitrary commands on the host | Broker allowlist; container cannot otherwise reach the host. |
| 8 | Orphaned host process / persistence | Broker tied to `mqyolo` PID; ephemeral home cleaned on exit. |
| 9 | Mutating "read-only" identity files via a writable alias | passwd/group re-bound read-only at the home path. |
| 10 | Secret leakage into batch jobs | `mqsandbox` forwards no API keys. |
| 11 | Accidental read-write exposure of unrelated data via a stray launch dir | `mqyolo` only starts from `/work/microbiome`, `$HOME`, `/scratch/microbiome/$USER`, or `/tmp` (§4.7). |

---

## 7. Verification

- `tests/test_mqyolo_sandbox.py` encodes the key invariants (writable set,
  read-only nested mounts, deny-list shadowing incl. canonical aliases, broker
  allowlist, forced `--sandbox`, fixed rw/ro path injection, rejection of
  override flags, broker PID lifetime, and the launch-directory restriction).
  **Run it after any change to the stack**:
  ```
  pixi run -e dev pytest tests/test_mqyolo_sandbox.py -v
  ```
  It is local-only (needs the HPC environment) and self-skips under CI.
- `mqyolo --print-guidance` and `--debug` (which prints the full `apptainer exec`
  bind list) are useful for auditing what a given launch actually exposes.

---

## 8. Residual risks, limitations, and non-goals

These are **not** mitigated by this design and must be managed separately:

- **Network egress is not restricted.** The container has outbound network
  (required for the AI API and package installs). Anything the agent can *read*
  — the working directory, operator-granted `--ro-paths`, and the read-only
  carve-outs (`/work/microbiome/db`, non-sensitive folders) — can in principle be
  sent off-node. The deny-list controls *what is readable*, not network flow.
  **Therefore: do not place sensitive data where the session can read it**
  (don't launch `mqyolo` with CWD inside sensitive data, and don't `--ro-paths`
  or mislabel sensitive folders as non-sensitive).
- **The non-sensitive-folder list is trust-based.** It comes from a
  human-maintained sheet. A mislabelled folder is exposed read-only to the
  session and its jobs (and thus to network egress). Keep the sheet accurate;
  the generator fails closed (only "not sensitive" is included) but cannot catch
  a wrong label.
- **CWD is fully writable.** The agent can modify or delete anything under the
  directory you launch it in. Choose the launch directory deliberately.
- **The spool is writable from inside the container.** Any in-container process
  can forge broker requests — but this only lets it invoke *allowlisted,
  sandbox-forced* commands as the user, which the agent is already permitted to
  do. It is not an escalation beyond the trust model.
- **No resource/DoS limits here.** The agent can submit many jobs; PBS quotas and
  fair-share are the backstop.
- **Container escape via Apptainer/kernel vulnerabilities is out of scope.** We
  rely on Apptainer's unprivileged execution model and the host's patching.
- **Unix permissions remain the floor.** The sandbox only ever *reduces* the
  launching user's access; it cannot protect data the user already cannot reach,
  and it is not a substitute for correct filesystem permissions.

---

## 9. Operating assumptions

- Apptainer/Singularity runs unprivileged with user namespaces.
- `/proc/mounts` enumerates every mountpoint that needs read-only protection.
- `SANDBOX_DENY_PATHS` covers the sensitive trees on this HPC; **new sensitive
  filesystems must be added there** (with their canonical aliases handled
  automatically).
- `getent`/SSSD is reachable on the host at launch time to build the passwd/group
  entry.

---

## 10. Change control

Any change to the files in §scope, to `SANDBOX_DENY_PATHS` /
`SANDBOX_RO_ALLOW_PATHS`, to the broker allowlist, or to the non-sensitive-folder
generator **must** be accompanied by a passing run of
`tests/test_mqyolo_sandbox.py` and, where the security behaviour changes, an
updated test and an update to this document.
