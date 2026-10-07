"""Tests for mqsub's project RPID resolution (-P/--rpid, $DEFAULT_RPID, group fallback, else none with a warning).

Group membership is faked by patching grp.getgrgid before running mqsub, so these
run anywhere (no PBS needed).
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
MQSUB = REPO / "bin" / "mqsub"

RUNNER = """
import grp, runpy, sys
group_name = sys.argv[1]
grp.getgrgid = lambda gid: type("G", (), {"gr_name": group_name})()
sys.argv = [sys.argv[2]] + sys.argv[3:]
runpy.run_path(sys.argv[0], run_name="__main__")
"""


def run_mqsub(*args, group="microbiome", rpid_env=None, tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "DEFAULT_RPID"}
    if rpid_env is not None:
        env["DEFAULT_RPID"] = rpid_env
    return subprocess.run(
        [sys.executable, "-c", RUNNER, group, str(MQSUB),
         "--dry-run", "--no-email", *args, "--", "echo", "hi"],
        text=True, capture_output=True, env=env, cwd=str(tmp_path),
    )


def pbs_project(result):
    lines = [l for l in (result.stdout + result.stderr).splitlines() if l.startswith("#PBS -P ")]
    assert len(lines) == 1, result.stdout + result.stderr
    return lines[0].split()[-1]


def test_member_gets_fallback(tmp_path):
    assert pbs_project(run_mqsub(tmp_path=tmp_path)) == "DFAZCB7230"


def test_non_member_without_rpid_warns_and_submits_without_one(tmp_path):
    result = run_mqsub(group="other", tmp_path=tmp_path)
    assert result.returncode == 0, result.stderr
    assert "WARNING: No project RPID set" in result.stderr
    assert "only available to members of the 'microbiome' group" in result.stderr
    assert "#PBS -q aqua" in result.stderr
    assert "#PBS -P" not in result.stdout + result.stderr


@pytest.mark.parametrize("group", ["microbiome", "other"])
def test_env_var_is_used(tmp_path, group):
    assert pbs_project(run_mqsub(group=group, rpid_env="ABCDEF1234", tmp_path=tmp_path)) == "ABCDEF1234"


@pytest.mark.parametrize("group", ["microbiome", "other"])
def test_flag_beats_env_var(tmp_path, group):
    result = run_mqsub("-P", "XYZABC9999", group=group, rpid_env="ABCDEF1234", tmp_path=tmp_path)
    assert pbs_project(result) == "XYZABC9999"


@pytest.mark.parametrize("bad", ["abcdef1234", "ABCDE12345", "ABCDEF123"])
def test_malformed_rpid_is_rejected(tmp_path, bad):
    result = run_mqsub("-P", bad, tmp_path=tmp_path)
    assert result.returncode == 1
    assert "Invalid project RPID" in result.stderr
