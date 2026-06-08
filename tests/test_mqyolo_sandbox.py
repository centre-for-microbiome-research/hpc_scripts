"""Local-only tests for the mqyolo sandbox + mqsub broker stack.

These exercise:
  - mqsub --sandbox wrapping the job command in mqsandbox
  - the host broker <-> container stub round-trip (mqsub-broker / mqbroker-stub)
  - the broker forcing --sandbox and the session's fixed --rw-paths, and refusing
    to let the container change them
  - the broker self-terminating when the watched parent PID dies
  - (when apptainer + the SIF are available) mqsandbox actually enforcing the
    read-only / read-write filesystem constraints

They are deliberately NOT run on GitHub Actions: they need python3.9, the HPC
filesystem layout, inotifywait and (for the container tests) apptainer + the
ai_tool.sif image, none of which exist on the CI runners. The whole module skips
when GITHUB_ACTIONS/CI is set; container tests skip individually when apptainer or
the SIF is missing, so the broker/wrapping logic can still be tested on a plain
login node without a built image.
"""

import contextlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
BIN = REPO / "bin"
MQSUB = BIN / "mqsub"
MQSANDBOX = BIN / "mqsandbox"
BROKER = BIN / "mqsub-broker"
STUB = BIN / "mqbroker-stub"

# Local-only: skip the entire module on CI / GitHub Actions.
pytestmark = pytest.mark.skipif(
    bool(os.environ.get("GITHUB_ACTIONS") or os.environ.get("CI")),
    reason="local-only: requires the HPC environment (python3.9, apptainer, SIF, mounts)",
)


def _sif_path():
    return os.environ.get("AI_TOOL_SIF") or str(REPO / "singularity" / "ai_tool.sif")


def _have_container():
    runtime = shutil.which("apptainer") or shutil.which("singularity")
    return bool(runtime) and os.path.exists(_sif_path())


requires_container = pytest.mark.skipif(
    not _have_container(),
    reason="apptainer/singularity or ai_tool.sif not available",
)


# ---------------------------------------------------------------------------
# mqsub --sandbox wrapping (no broker, no container needed)
# ---------------------------------------------------------------------------
def _mqsub_dry_run(*extra):
    """Run mqsub with --dry-run and return combined stdout+stderr."""
    cmd = [sys.executable, str(MQSUB), "--dry-run", "-t", "1", "--hours", "1", *extra]
    p = subprocess.run(cmd, text=True, capture_output=True)
    return p.returncode, p.stdout + p.stderr


def test_mqsub_sandbox_wraps_command():
    rc, out = _mqsub_dry_run("--sandbox", "--", "echo", "hello", "world")
    assert rc == 0, out
    assert "mqsandbox" in out
    assert '--cwd "$PWD"' in out
    assert "bash -c 'echo hello world'" in out


def test_mqsub_without_sandbox_is_unwrapped():
    rc, out = _mqsub_dry_run("--no-executable-check", "--", "echo", "hi")
    assert rc == 0, out
    assert "mqsandbox" not in out


def test_mqsub_sandbox_rw_paths_appear_in_wrapper():
    rc, out = _mqsub_dry_run(
        "--sandbox",
        "--sandbox-rw-paths", "/data/refs",
        "--sandbox-rw-paths", "/scratch/x",
        "--", "echo", "hi",
    )
    assert rc == 0, out
    assert "--rw-paths /data/refs /scratch/x" in out


def test_mqsub_sandbox_rejects_command_file_chunking():
    # --sandbox with chunking should error clearly rather than silently misbehave.
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        f.write("echo one\necho two\n")
        cmdfile = f.name
    try:
        p = subprocess.run(
            [sys.executable, str(MQSUB), "--dry-run", "--sandbox",
             "--command-file", cmdfile, "--chunk-num", "1"],
            text=True, capture_output=True,
        )
        assert p.returncode != 0
        assert "sandbox" in (p.stdout + p.stderr).lower()
    finally:
        os.unlink(cmdfile)


# ---------------------------------------------------------------------------
# Broker <-> stub round-trip helpers
# ---------------------------------------------------------------------------
@contextlib.contextmanager
def running_broker(rw_paths=(), watch_pid=None, interval=1):
    """Start a broker (watching a throwaway parent unless watch_pid given),
    yield (spool_dir, shim_dir, broker_proc, dummy_proc). Cleans up on exit."""
    spool = tempfile.mkdtemp(prefix="mqbroker_spool_")
    shim = tempfile.mkdtemp(prefix="mqbroker_shim_")

    dummy = None
    if watch_pid is None:
        dummy = subprocess.Popen(["sleep", "120"])
        watch_pid = dummy.pid

    args = [str(BROKER), "--spool", spool, "--watch-pid", str(watch_pid),
            "--watch-interval", str(interval)]
    for p in rw_paths:
        args += ["--rw-path", p]
    broker = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(0.4)  # let the broker create the req dir / start watching
        yield spool, shim, broker, dummy
    finally:
        for proc in (broker, dummy):
            if proc and proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
        shutil.rmtree(spool, ignore_errors=True)
        shutil.rmtree(shim, ignore_errors=True)


def _stub_as(shim, name):
    """Create a stub symlink named `name` in shim dir, return its path."""
    link = os.path.join(shim, name)
    if not os.path.exists(link):
        os.symlink(os.path.realpath(STUB), link)
    return link


def _run_stub(stub_path, spool, *argv, timeout=60):
    env = {**os.environ, "MQBROKER_SPOOL": spool}
    p = subprocess.run([stub_path, *argv], text=True, capture_output=True,
                       env=env, timeout=timeout)
    return p.returncode, p.stdout + p.stderr


def test_broker_roundtrip_forces_sandbox():
    with running_broker() as (spool, shim, _broker, _dummy):
        mqsub = _stub_as(shim, "mqsub")
        rc, out = _run_stub(mqsub, spool, "--dry-run", "-t", "1", "--hours", "1",
                            "--", "echo", "hi")
        assert rc == 0, out
        # The job was wrapped in mqsandbox even though the container never asked.
        assert "mqsandbox" in out
        assert "bash -c 'echo hi'" in out


def test_broker_injects_fixed_rw_paths():
    with running_broker(rw_paths=["/data/refs", "/scratch/shared"]) as (spool, shim, *_):
        mqsub = _stub_as(shim, "mqsub")
        rc, out = _run_stub(mqsub, spool, "--dry-run", "-t", "1", "--hours", "1",
                            "--", "mytool", "--out", "result")
        assert rc == 0, out
        assert "--rw-paths /data/refs /scratch/shared" in out
        # The command must not be swallowed by the rw-paths flag.
        assert "bash -c 'mytool --out result'" in out


def test_broker_rejects_container_set_rw_paths():
    with running_broker(rw_paths=["/data/refs"]) as (spool, shim, *_):
        mqsub = _stub_as(shim, "mqsub")
        rc, out = _run_stub(mqsub, spool, "--sandbox-rw-paths", "/", "--", "echo", "hi")
        assert rc == 126, out
        assert "not permitted" in out


def test_broker_rejects_no_sandbox():
    with running_broker() as (spool, shim, *_):
        mqsub = _stub_as(shim, "mqsub")
        rc, out = _run_stub(mqsub, spool, "--no-sandbox", "--", "echo", "hi")
        assert rc == 126, out
        assert "not permitted" in out


def test_broker_rejects_non_allowlisted_command():
    with running_broker() as (spool, shim, *_):
        evil = _stub_as(shim, "evilcmd")
        rc, out = _run_stub(evil, spool, "whatever")
        assert rc == 126, out
        assert "not permitted" in out


def test_broker_propagates_nonzero_exit_and_stderr():
    with running_broker() as (spool, shim, *_):
        mqsub = _stub_as(shim, "mqsub")
        # mqsub with no command errors out with a non-zero exit.
        rc, out = _run_stub(mqsub, spool, "--dry-run")
        assert rc != 0
        assert "Must specify" in out


def test_broker_exits_when_parent_dies():
    dummy = subprocess.Popen(["sleep", "120"])
    try:
        with running_broker(watch_pid=dummy.pid, interval=1) as (spool, shim, broker, _):
            assert broker.poll() is None  # alive while parent alive
            dummy.kill()
            dummy.wait(timeout=5)
            # Broker should notice within a few watch intervals and exit.
            deadline = time.time() + 10
            while time.time() < deadline and broker.poll() is None:
                time.sleep(0.3)
            assert broker.poll() is not None, "broker did not exit after parent died"
    finally:
        if dummy.poll() is None:
            dummy.kill()


# ---------------------------------------------------------------------------
# mqsandbox actually enforcing the filesystem constraints (needs the container)
# ---------------------------------------------------------------------------
def _run_in_sandbox(cwd, script, rw_paths=()):
    args = [str(MQSANDBOX), "--cwd", str(cwd)]
    for p in rw_paths:
        args += ["--rw-paths", p]
    args += ["--", "bash", "-c", script]
    p = subprocess.run(args, text=True, capture_output=True, timeout=120)
    return p.returncode, p.stdout + p.stderr


@requires_container
def test_mqsandbox_enforces_constraints():
    # CWD lives under the lustre /mnt mount, proving the rw CWD bind shadows the
    # read-only mount bind (the bug class that made the repo writable).
    cwd = tempfile.mkdtemp(prefix="mqs_cwd_", dir=str(REPO))
    repo_marker = str(REPO / "MQS_RO_MARKER")
    home_marker = os.path.join(os.path.expanduser("~"), "MQS_RO_MARKER")
    script = (
        'echo -n "cwd:"; (echo x > ./w && echo OK || echo FAIL); '
        'echo -n "tmp:"; (touch /tmp/_mqs_$$ && rm -f /tmp/_mqs_$$ && echo OK || echo FAIL); '
        'echo -n "read-repo:"; (head -1 %s >/dev/null 2>&1 && echo OK || echo FAIL); '
        'echo -n "repo:"; (echo x > %s 2>/dev/null && echo WRITABLE || echo RO); '
        'echo -n "home:"; (echo x > %s 2>/dev/null && echo WRITABLE || echo RO)'
        % (str(REPO / "README.md"), repo_marker, home_marker)
    )
    try:
        rc, out = _run_in_sandbox(cwd, script)
        assert rc == 0, out
        assert "cwd:OK" in out
        assert "tmp:OK" in out
        assert "read-repo:OK" in out
        assert "repo:RO" in out, out
        assert "home:RO" in out, out
    finally:
        shutil.rmtree(cwd, ignore_errors=True)
        for m in (repo_marker, home_marker):
            with contextlib.suppress(OSError):
                os.unlink(m)


@requires_container
def test_mqsandbox_rw_path_is_writable():
    cwd = tempfile.mkdtemp(prefix="mqs_cwd_", dir=str(REPO))
    rwdir = tempfile.mkdtemp(prefix="mqs_rw_", dir=str(REPO))
    marker = os.path.join(rwdir, "written")
    try:
        rc, out = _run_in_sandbox(
            cwd, 'echo x > %s && echo RWOK || echo RWFAIL' % marker, rw_paths=[rwdir]
        )
        assert rc == 0, out
        assert "RWOK" in out
        assert os.path.exists(marker)
    finally:
        shutil.rmtree(cwd, ignore_errors=True)
        shutil.rmtree(rwdir, ignore_errors=True)
