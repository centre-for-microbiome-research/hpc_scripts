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
import shlex
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


def test_mqsub_sandbox_ro_paths_appear_in_wrapper():
    rc, out = _mqsub_dry_run(
        "--sandbox",
        "--sandbox-ro-paths", "/work/microbiome/shared",
        "--sandbox-ro-paths", "/data/atlas",
        "--", "echo", "hi",
    )
    assert rc == 0, out
    assert "--ro-paths /work/microbiome/shared /data/atlas" in out


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


MQYOLO = BIN / "mqyolo"


def _print_guidance(extra_env=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("PBS") and k != "NCPUS"}
    env["AI_TOOL_SIF"] = "/nonexistent.sif"
    if extra_env:
        env.update(extra_env)
    p = subprocess.run([str(MQYOLO), "--print-guidance"], text=True,
                       capture_output=True, env=env)
    return p.returncode, p.stdout, p.stderr


def test_mqyolo_print_guidance_login_node():
    # On a login node (no PBS_JOBID): offload heavy work to the queue.
    rc, out, err = _print_guidance()
    assert rc == 0, err
    assert "login node" in out
    assert "Offload heavy work" in out
    assert "snakemake --profile aqua" in out


def test_mqyolo_print_guidance_pbs_job():
    # Inside a PBS job: run heavy work directly within the allocation.
    rc, out, err = _print_guidance({"PBS_JOBID": "123.aqua",
                                    "PBS_ENVIRONMENT": "PBS_INTERACTIVE",
                                    "NCPUS": "24"})
    assert rc == 0, err
    assert "inside a PBS job" in out
    assert "24 CPUs" in out
    assert "--threads 24" in out
    assert "finite budget" in out
    # Larger jobs should still go to the queue even inside an interactive session.
    assert "submit it to the batch queue" in out
    assert "snakemake --profile aqua" in out


# ---------------------------------------------------------------------------
# Broker <-> stub round-trip helpers
# ---------------------------------------------------------------------------
@contextlib.contextmanager
def running_broker(rw_paths=(), ro_paths=(), watch_pid=None, interval=1):
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
    for p in ro_paths:
        args += ["--ro-path", p]
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


def test_broker_injects_fixed_ro_paths():
    with running_broker(ro_paths=["/work/microbiome/shared", "/data/atlas"]) as (spool, shim, *_):
        mqsub = _stub_as(shim, "mqsub")
        rc, out = _run_stub(mqsub, spool, "--dry-run", "-t", "1", "--hours", "1",
                            "--", "mytool", "--out", "result")
        assert rc == 0, out
        assert "--ro-paths /work/microbiome/shared /data/atlas" in out
        # The command must not be swallowed by the ro-paths flag.
        assert "bash -c 'mytool --out result'" in out


def test_broker_rejects_container_set_rw_paths():
    with running_broker(rw_paths=["/data/refs"]) as (spool, shim, *_):
        mqsub = _stub_as(shim, "mqsub")
        rc, out = _run_stub(mqsub, spool, "--sandbox-rw-paths", "/", "--", "echo", "hi")
        assert rc == 126, out
        assert "not permitted" in out


def test_broker_rejects_container_set_ro_paths():
    with running_broker(ro_paths=["/data/refs"]) as (spool, shim, *_):
        mqsub = _stub_as(shim, "mqsub")
        rc, out = _run_stub(mqsub, spool, "--sandbox-ro-paths", "/", "--", "echo", "hi")
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
# Shim dir must win on PATH even though the user's bashrc prepends the real
# hpc_scripts bin dir (sandbox_write_shim_bashrc in _sandbox_common.bash).
# ---------------------------------------------------------------------------
SANDBOX_LIB = BIN / "_sandbox_common.bash"


def test_shim_bashrc_keeps_shim_first_on_path(tmp_path):
    shim = "/container_home/.mqyolo/shims"
    # A "real" bashrc that prepends the real hpc_scripts bin dir, as the user's does.
    real = tmp_path / "real_bashrc"
    real.write_text('export PATH="/work/microbiome/sw/hpc_scripts/bin:$PATH"\n')
    dest = tmp_path / "dest_bashrc"
    # Build dest via the actual library function, then source it and inspect PATH.
    script = (
        'source %s; '
        'sandbox_write_shim_bashrc %s %s %s; '
        'PATH=/usr/bin:/bin; source %s; '
        'printf "%%s\\n" "${PATH%%%%:*}"'
        % (SANDBOX_LIB, str(dest), str(real), shim, str(dest))
    )
    p = subprocess.run(["bash", "-c", script], text=True, capture_output=True)
    assert p.returncode == 0, p.stderr
    assert p.stdout.strip() == shim, p.stdout


def test_shim_bashrc_does_not_write_through_symlink(tmp_path):
    # dest is a symlink to a precious file; the function must replace the symlink,
    # not clobber the target (which is the real ~/.bashrc in production).
    precious = tmp_path / "precious_real_bashrc"
    precious.write_text("ORIGINAL\n")
    dest = tmp_path / "dest_bashrc"
    dest.symlink_to(precious)
    script = "source %s; sandbox_write_shim_bashrc %s '' /some/shim" % (SANDBOX_LIB, str(dest))
    p = subprocess.run(["bash", "-c", script], text=True, capture_output=True)
    assert p.returncode == 0, p.stderr
    assert precious.read_text() == "ORIGINAL\n", "function wrote through the symlink!"
    assert not dest.is_symlink()
    assert "/some/shim" in dest.read_text()


# ---------------------------------------------------------------------------
# pixi_cmr_init.py (and any other repo tools) must be staged onto PATH inside the
# container: sandbox_stage_repo_tools symlinks them into mqyolo's tools dir, and
# the shim ~/.bashrc keeps that dir ahead of the user's bashrc-prepended dirs.
# ---------------------------------------------------------------------------
PIXI_CMR_INIT = BIN / "pixi_cmr_init.py"


def test_pixi_cmr_init_present_in_repo():
    # mqyolo stages this from the repo bin; it must actually be there.
    assert PIXI_CMR_INIT.exists(), "pixi_cmr_init.py missing from repo bin"


def test_stage_repo_tools_symlinks_pixi_cmr_init(tmp_path):
    tools = tmp_path / "tools"
    script = (
        "source %s; sandbox_stage_repo_tools %s %s; readlink -f %s/pixi_cmr_init.py"
        % (SANDBOX_LIB, shlex.quote(str(tools)), shlex.quote(str(BIN)),
           shlex.quote(str(tools)))
    )
    p = subprocess.run(["bash", "-c", script], text=True, capture_output=True)
    assert p.returncode == 0, p.stderr
    # The staged symlink points at the repo's real pixi_cmr_init.py.
    assert p.stdout.strip() == os.path.realpath(str(PIXI_CMR_INIT)), p.stdout


def test_pixi_cmr_init_resolves_on_path_via_shim_bashrc(tmp_path):
    # Stage the repo tool, then build the shim ~/.bashrc with the tools dir on the
    # PATH prefix (exactly as mqyolo does). Even though a "real" bashrc prepends a
    # decoy dir that ALSO contains a pixi_cmr_init.py (mimicking the deployed copy
    # under /work/microbiome/sw), the staged repo copy must win.
    tools = tmp_path / "tools"
    deployed = tmp_path / "deployed"
    deployed.mkdir()
    decoy = deployed / "pixi_cmr_init.py"
    decoy.write_text("#!/bin/sh\necho decoy\n")
    decoy.chmod(0o755)
    real = tmp_path / "real_bashrc"
    real.write_text('export PATH="%s:$PATH"\n' % deployed)
    dest = tmp_path / "dest_bashrc"
    script = (
        "source %s; "
        "sandbox_stage_repo_tools %s %s; "
        "sandbox_write_shim_bashrc %s %s %s; "
        "PATH=/usr/bin:/bin; source %s; "
        "command -v pixi_cmr_init.py"
        % (SANDBOX_LIB, shlex.quote(str(tools)), shlex.quote(str(BIN)),
           shlex.quote(str(dest)), shlex.quote(str(real)), shlex.quote(str(tools)),
           shlex.quote(str(dest)))
    )
    p = subprocess.run(["bash", "-c", script], text=True, capture_output=True)
    assert p.returncode == 0, p.stderr
    assert p.stdout.strip() == str(tools / "pixi_cmr_init.py"), p.stdout


# ---------------------------------------------------------------------------
# Anti-exfiltration deny-list (sandbox_path_denied + sandbox_build_binds).
# These run anywhere — no container needed.
# ---------------------------------------------------------------------------
def test_sandbox_path_denied_classification():
    cases = [
        ("/scratch", "D"),
        ("/scratch/foo/bar", "D"),
        ("/work/microbiome", "D"),
        ("/work/microbiome/someuser", "D"),
        ("/work/microbiome/sw", "A"),
        ("/work/microbiome/sw/hpc_scripts/bin", "A"),
        ("/work/microbiome/db", "A"),
        ("/work/microbiome/db/gtdb", "A"),
        ("/work", "A"),
        ("/home", "A"),
        ("/mnt/hpccs01/home/x", "A"),
    ]
    script = "source %s\n" % SANDBOX_LIB
    for path, _ in cases:
        script += 'sandbox_path_denied %s && echo D || echo A\n' % path
    p = subprocess.run(["bash", "-c", script], text=True, capture_output=True)
    assert p.returncode == 0, p.stderr
    got = p.stdout.split()
    want = [c[1] for c in cases]
    assert got == want, "got %s want %s" % (got, want)


def _build_binds(cwd, rw_paths=(), ro_paths=()):
    """Drive sandbox_build_binds and return the list of `src:dst:mode` bind specs."""
    args = " ".join(shlex.quote(p) for p in rw_paths)
    if ro_paths:
        args += " -- " + " ".join(shlex.quote(p) for p in ro_paths)
    script = (
        "set -euo pipefail\n"
        "source %s\n"
        "CONTAINER_HOME=$(mktemp -d)\n"
        "BIND_ARGS=()\n"
        "sandbox_build_binds %s %s\n"
        'for a in "${BIND_ARGS[@]}"; do [[ "$a" == --bind ]] || printf "%%s\\n" "$a"; done\n'
        "rm -rf \"$CONTAINER_HOME\"\n"
        % (SANDBOX_LIB, shlex.quote(str(cwd)), args)
    )
    p = subprocess.run(["bash", "-c", script], text=True, capture_output=True)
    assert p.returncode == 0, p.stderr
    return p.stdout.splitlines()


def test_build_binds_excludes_scratch_and_microbiome(tmp_path):
    binds = _build_binds(tmp_path)
    # /scratch is never bound from its real path; /work/microbiome is never bound
    # wholesale from its real path either (only the shadow + carved-out exceptions).
    assert "/scratch:/scratch:ro" not in binds, binds
    assert "/work/microbiome:/work/microbiome:ro" not in binds, binds
    assert not any(b.startswith("/scratch:") for b in binds), binds
    # The denied /work/microbiome tree is shadowed by an empty dir bound at the
    # same path (source is under the ephemeral CONTAINER_HOME, not the real tree).
    shadow = [b for b in binds if b.endswith(":/work/microbiome:ro")]
    assert len(shadow) == 1 and "_denied_work_microbiome" in shadow[0], binds
    # The read-only exceptions are re-bound from their real paths.
    assert "/work/microbiome/sw:/work/microbiome/sw:ro" in binds, binds
    assert "/work/microbiome/db:/work/microbiome/db:ro" in binds, binds


def test_build_binds_ro_paths_are_bound_readonly(tmp_path):
    rodir = tmp_path / "ro_extra"
    rodir.mkdir()
    binds = _build_binds(tmp_path, ro_paths=[str(rodir)])
    real = os.path.realpath(str(rodir))
    assert "%s:%s:ro" % (real, real) in binds, binds


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
def test_mqsandbox_hides_denied_paths():
    # Inside the sandbox /scratch must not exist at all, and /work/microbiome must
    # expose ONLY its sw and db sub-paths (the carved-out read-only exceptions) —
    # everything else under it is hidden behind the shadow dir. This holds
    # regardless of what the host trees actually contain.
    cwd = tempfile.mkdtemp(prefix="mqs_cwd_", dir=str(REPO))
    script = (
        'echo -n "scratch:"; ([[ -e /scratch ]] && echo PRESENT || echo ABSENT); '
        'echo -n "microbiome:"; (ls -1 /work/microbiome 2>/dev/null | sort | tr "\\n" "," )'
        '; echo'
    )
    try:
        rc, out = _run_in_sandbox(cwd, script)
        assert rc == 0, out
        assert "scratch:ABSENT" in out, out
        assert "microbiome:db,sw," in out, out
    finally:
        shutil.rmtree(cwd, ignore_errors=True)


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
