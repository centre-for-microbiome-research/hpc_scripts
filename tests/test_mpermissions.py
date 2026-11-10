"""Tests for the ACL-free (weka) path of bin/mpermissions.

MPERMISSIONS_WEKA_PREFIX points the weka branch at a temp dir, so these run
anywhere. The point of that branch is to chgrp/chmod only where something
differs: chmod(2)/chown(2) fail for non-owners even when they would change
nothing, so an unconditional run could never come back clean on a tree
another user installed.
"""
import grp
import os
import stat
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "bin" / "mpermissions"
GROUP = grp.getgrgid(os.getgid()).gr_name


def run_mpermissions(folder, *extra):
    env = dict(os.environ, MPERMISSIONS_WEKA_PREFIX=str(folder))
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "-g", GROUP, *extra, str(folder)],
        env=env, capture_output=True, text=True)
    return result, result.stdout + result.stderr


def mode(path):
    return stat.S_IMODE(os.lstat(path).st_mode)


def make_tree(root):
    (root / "dir").mkdir()
    (root / "dir" / "file").write_text("x")
    (root / "dir" / "tool").write_text("#!/bin/sh\n")
    (root / "link").symlink_to("dir/file")
    os.chmod(root / "dir" / "file", 0o600)
    os.chmod(root / "dir" / "tool", 0o700)
    os.chmod(root / "dir", 0o700)
    os.chmod(root, 0o700)


def test_other_read_modes(tmp_path):
    make_tree(tmp_path)
    result, out = run_mpermissions(tmp_path, "--other-read")
    assert result.returncode == 0, out
    assert "CMD FAILED" not in out
    assert mode(tmp_path) == 0o2775
    assert mode(tmp_path / "dir") == 0o2775
    assert mode(tmp_path / "dir" / "file") == 0o664
    assert mode(tmp_path / "dir" / "tool") == 0o775


def test_default_removes_other_access(tmp_path):
    make_tree(tmp_path)
    os.chmod(tmp_path / "dir" / "file", 0o607)
    result, out = run_mpermissions(tmp_path)
    assert "CMD FAILED" not in out
    assert mode(tmp_path / "dir") == 0o2770
    assert mode(tmp_path / "dir" / "file") == 0o660
    assert mode(tmp_path / "dir" / "tool") == 0o770


def test_no_group_write(tmp_path):
    make_tree(tmp_path)
    run_mpermissions(tmp_path, "--other-read", "--no-group-write")
    # Directories keep group traverse (the old chmod "g+r-x" removed it).
    assert mode(tmp_path / "dir") == 0o2755
    assert mode(tmp_path / "dir" / "file") == 0o644
    assert mode(tmp_path / "dir" / "tool") == 0o755


def test_second_run_changes_nothing(tmp_path):
    make_tree(tmp_path)
    run_mpermissions(tmp_path, "--other-read")
    result, out = run_mpermissions(tmp_path, "--other-read")
    assert "0 path(s) needed chgrp, 0 needed chmod" in out, out


def test_already_correct_paths_are_not_touched(tmp_path):
    """An already-correct path must get no chmod/chown call at all, which is
    what lets a non-owner pass over it. Observable as an unchanged ctime,
    since even a no-op chmod updates it."""
    make_tree(tmp_path)
    run_mpermissions(tmp_path, "--other-read")
    before = os.lstat(tmp_path / "dir" / "file").st_ctime_ns
    run_mpermissions(tmp_path, "--other-read")
    assert os.lstat(tmp_path / "dir" / "file").st_ctime_ns == before


def test_dry_run_changes_nothing(tmp_path):
    make_tree(tmp_path)
    result, out = run_mpermissions(tmp_path, "--other-read", "--dry-run")
    assert "Would chmod" in out
    assert mode(tmp_path / "dir" / "file") == 0o600


def test_symlink_is_not_followed(tmp_path):
    make_tree(tmp_path)
    outside = tmp_path.parent / (tmp_path.name + "-outside")
    outside.write_text("x")
    os.chmod(outside, 0o600)
    (tmp_path / "escape").symlink_to(outside)
    run_mpermissions(tmp_path, "--other-read")
    assert mode(outside) == 0o600


def test_unknown_group_fails(tmp_path):
    env = dict(os.environ, MPERMISSIONS_WEKA_PREFIX=str(tmp_path))
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "-g", "no-such-group-xyz", str(tmp_path)],
        env=env, capture_output=True, text=True)
    assert result.returncode == 1
    assert "CMD FAILED" in result.stderr
