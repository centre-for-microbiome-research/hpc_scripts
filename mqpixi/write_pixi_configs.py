#!/usr/bin/env python3
"""Write .pixi/config.toml inside packages that embed their own pixi workspace.

Usage: python write_pixi_configs.py <env-name> <package-name>
Example: python write_pixi_configs.py aviary-v0-13-0 aviary

Sets detached-environments = false so the package uses the existing conda env
rather than creating a separate one.
"""
import sys
import json
import tomllib
import pathlib

env_name, pkg_name = sys.argv[1], sys.argv[2]
parent_pkg = sys.argv[3] if len(sys.argv) > 3 else None

env_path = pathlib.Path(f".pixi/envs/{env_name}")
if not env_path.exists():
    print(f"env {env_name} not found, skipping")
    sys.exit(0)

if parent_pkg:
    # Nested case: env_name/lib/python*/site-packages/parent_pkg/.pixi/envs/pkg_name/lib/python*/site-packages/pkg_name
    parent_dirs = list(env_path.glob(f"lib/python*/site-packages/{parent_pkg}"))
    if not parent_dirs:
        raise FileNotFoundError(f"Package {parent_pkg} not found in {env_name}")
    nested_env = parent_dirs[0] / ".pixi" / "envs" / pkg_name
    if not nested_env.exists():
        raise FileNotFoundError(f"Nested env {pkg_name} not found at {nested_env}")
    pkg_dirs = list(nested_env.glob(f"lib/python*/site-packages/{pkg_name}"))
    if not pkg_dirs:
        raise FileNotFoundError(f"Package {pkg_name} not found in nested env {nested_env}")
else:
    pkg_dirs = list(env_path.glob(f"lib/python*/site-packages/{pkg_name}"))
    if not pkg_dirs:
        raise FileNotFoundError(f"Package {pkg_name} not found in {env_name}")

d = pkg_dirs[0] / ".pixi"
p = d / "config.toml"
d.mkdir(exist_ok=True)

cfg = tomllib.load(open(p, "rb")) if p.exists() else {}
cfg["detached-environments"] = False

with open(p, "w") as f:
    for k, v in cfg.items():
        f.write(k + " = " + json.dumps(v) + "\n")

print(f"wrote {p}")
