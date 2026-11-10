# Adding a new mqpixi environment

`pixi.toml` here defines every tool environment `mqpixi` can run (100+, each
its own pixi `[environments]` entry). This walks through adding one, using
the `aviary-v0-14-0` and `phagespotter-v0-3-0` additions in PR #91 as the
worked examples.

## 1. Add to `pixi.toml`

Environment names are `<tool>-v<major>-<minor>-<patch>` (dots become dashes -
a literal `.` in a TOML table key starts a nested table, which breaks the
whole block).

A plain tool (no extra build step) needs just two things:

```toml
# ---------- mytool-v1-2-3 ----------
[feature.mytool-v1-2-3.dependencies]
mytool = "==1.2.3"

[feature.mytool-v1-2-3.activation]
env = { PYTHONNOUSERSITE = "1" }
```

`PYTHONNOUSERSITE = "1"` is set on every environment in this file - without
it, a user's own `~/.local` site-packages can leak into the env. Add any
tool-specific env vars (DB paths etc.) to the same `env = { ... }` table.

If the package isn't on conda-forge/bioconda/PyPI (e.g. a one-off wheel),
pin it with `pypi-dependencies` and a `path =` instead, as done for
`phagespotter-v0-3-0`:

```toml
[feature.phagespotter-v0-3-0.pypi-dependencies]
phagespotter = { path = "/work/microbiome/sw/phagespotter/0.3.0/phagespotter-0.3.0-py3-none-any.whl" }
```

If it genuinely can't be pinned at all yet, leave a commented-out `# DISABLED
- <reason>` stub (see the git history of this file before PR #91) so the next
person doesn't have to rediscover why.

If the tool needs a build step after its conda deps are installed (aviary
and binchicken do, to build their own nested pixi envs) you additionally
need, in four places:

1. `[feature.mytool-v1-2-3.tasks]` with a `postinstall-base` task (see
   `aviary-v0-14-0` for the `umask 002`/`CONDA_OVERRIDE_CUDA` pattern - both
   are explained inline there).
2. `[tasks.postinstall-mytool-config-v1-2-3]` if the tool manages its own
   nested `.pixi/config.toml` (uses `write_pixi_configs.py`; look at
   `postinstall-aviary-config-v0-14-0`).
3. `[tasks.postinstall-mytool-v1-2-3]`, depending on
   `postinstall-activate-vars` and the env's `postinstall-base` task.
4. Add `"postinstall-mytool-v1-2-3"` to the `depends-on` list in the single
   aggregate `[tasks.postinstall]` task, or `deploy_to_production.sh`'s
   blanket `pixi run postinstall` will never reach it.

Finally, add the environment itself to `[environments]`, keeping the list
alphabetically sorted:

```toml
mytool-v1-2-3 = { features = ["mytool-v1-2-3"], no-default-feature = true }
```

## 2. `pixi install`

Must run **outside** the mqyolo/opencode sandbox - `mqpixi/.pixi` is
symlinked to the shared `/pkg/cmr/mqpixi/.pixi` store, and `/pkg` is
read-only inside the sandbox.

```bash
cd mqpixi
pixi install -e mytool-v1-2-3
pixi run postinstall-mytool-v1-2-3   # only if you added a postinstall task in step 1
```

This is what actually resolves and updates `pixi.lock` (it doesn't exist
until you run this) - commit the result together with your `pixi.toml`
change, since `deploy_to_production.sh` installs with `--frozen` and needs
the lock to already satisfy the manifest.

While you're here, smoke-test that the env actually works, e.g.
`pixi run -e mytool-v1-2-3 mytool --version`.

## 3. `deploy_to_production.sh`

This is run by an admin after the PR is merged, from a normal login shell
(also outside the sandbox, for the same `/pkg` reason):

```bash
~/src/hpc_scripts/deploy_to_production.sh --fix-permissions
```

Against the production checkout (default `/work/microbiome/sw/hpc_scripts`),
this re-pulls merged `main`, runs `pixi install -a --frozen` for every
environment, fixes permissions group-wide, and runs the aggregate
`postinstall`. `--fix-permissions` skips any environment that already has a
`permissions.done` marker from a previous clean run. To force specific envs to
be re-checked, name them instead (repeatable; implies `--fix-permissions` and
only fixes the named envs):

```bash
~/src/hpc_scripts/deploy_to_production.sh --fix-permissions-env mytool-v1-2-3
```

## 4. Commit and PR

- Branch off an up-to-date `main` (`git fetch upstream && git merge --ff-only
  upstream/main` first if `upstream` is configured).
- Commit `pixi.toml` and `pixi.lock` together - a lock update with no
  matching manifest change (or vice versa) is confusing to review and breaks
  the `--frozen` install if someone deploys from an in-between state.
- Open the PR against upstream's default branch, head
  `<your-github-username>:<branch>`.
