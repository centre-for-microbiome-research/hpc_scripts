#!/usr/bin/env python3
"""Write pixi activation env vars to conda activate.d scripts for each environment.

Ensures vars are applied when environments are activated via `conda activate`
directly (bypassing pixi's own activation mechanism, e.g. in mqsub jobs).

Run via: pixi run postinstall-activate-vars
"""
import tomllib
import pathlib

cfg = tomllib.load(open("pixi.toml", "rb"))
feats = cfg.get("feature", {})

for env, ecfg in cfg.get("environments", {}).items():
    env_prefix = pathlib.Path(".pixi/envs") / env
    if not env_prefix.exists():
        continue

    activate_d = env_prefix / "etc/conda/activate.d"
    activate_d.mkdir(parents=True, exist_ok=True)

    # Always isolate from user site-packages, then overlay per-feature vars
    vs = {"PYTHONNOUSERSITE": "1"}
    for feat_name in ecfg.get("features", []):
        vs.update(feats.get(feat_name, {}).get("activation", {}).get("env", {}))

    # Nested envs (e.g. binchicken's embedded .pixi/envs/*): programs run from them
    # (binchicken -> aviary -> snakemake --profile aqua) submit jobs whose script does
    # `conda activate $CONDA_PREFIX` of the *nested* prefix, so the outer env's vars
    # (PIXI_FROZEN, PIXI_NO_INSTALL) never reach the cluster job otherwise.
    targets = [activate_d]
    targets += [
        p / "etc/conda/activate.d"
        for p in env_prefix.glob("lib/python*/site-packages/*/.pixi/envs/*")
        if (p / "conda-meta").is_dir()
    ]

    for d in targets:
        d.mkdir(parents=True, exist_ok=True)
        script_path = d / "pixi_vars.sh"
        script_path.write_text(
            "\n".join(f'export {k}="{v}"' for k, v in vs.items()) + "\n"
        )
        print(f"wrote {script_path}")
