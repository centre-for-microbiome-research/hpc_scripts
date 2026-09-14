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
import json
import os
import re
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


def _fake_qsub_path(tmp_path):
    """A PATH string with a stub `qsub` first, so mqyolo detects a reachable batch
    queue (login/pbs) regardless of where the test itself runs (e.g. inside an
    mqyolo container, where the real qsub is absent)."""
    fakebin = tmp_path / "qsubbin"
    fakebin.mkdir()
    qsub = fakebin / "qsub"
    qsub.write_text("#!/bin/sh\nexit 0\n")
    qsub.chmod(0o755)
    return f"{fakebin}:/usr/bin:/bin"


def test_mqyolo_print_guidance_login_node(tmp_path):
    # On a login node (no PBS_JOBID, batch queue reachable via qsub): offload heavy
    # work to the queue.
    rc, out, err = _print_guidance({"PATH": _fake_qsub_path(tmp_path)})
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


def test_mqyolo_print_guidance_local_no_queue():
    # Off the batch queue (no `qsub` reachable, e.g. a workstation that only
    # sshfs-mounts aqua): the AI must be told there is no queue and to run work
    # locally, NOT to offload to mqsub. Simulate by restricting PATH to the base
    # system dirs so the PBS `qsub` (installed elsewhere) is not found.
    rc, out, err = _print_guidance({"PATH": "/usr/bin:/bin"})
    assert rc == 0, err
    assert "no batch queue" in out
    assert "run everything directly" in out.lower()
    # It must not fall through to the queue-oriented login/PBS guidance.
    assert "login node" not in out
    assert "Offload heavy work" not in out
    assert "profile aqua" not in out


def test_mqyolo_codex_uses_current_auto_mode_flag(tmp_path):
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    fake_apptainer = fakebin / "apptainer"
    fake_apptainer.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\"\n")
    fake_apptainer.chmod(0o755)
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    fake_sif = tmp_path / "ai_tool.sif"
    fake_sif.write_text("")
    env = {
        **os.environ,
        "PATH": f"{fakebin}:{os.environ['PATH']}",
        "HOME": str(fake_home),
        "AI_TOOL_SIF": str(fake_sif),
    }

    p = subprocess.run(
        [str(MQYOLO), "--no-broker", "codex"],
        text=True,
        capture_output=True,
        env=env,
        # Launch from $HOME so the launch-directory restriction is satisfied.
        cwd=str(fake_home),
    )
    assert p.returncode == 0, p.stderr
    out = p.stdout + p.stderr
    assert "--dangerously-bypass-approvals-and-sandbox" in out
    assert "--full-auto" not in out
    assert "--search" in out
    assert "PATH=/container_home/.mqyolo/tools:/usr/local/bin:/root/.local/bin:/usr/bin:/bin" in out


def test_mqyolo_opencode_uses_auto_flag_and_binds_its_dirs(tmp_path):
    # opencode is auto-approved with --auto, and ALL FOUR directories it keeps
    # state in (config + global AGENTS.md, auth/session storage, session state and
    # cache) are bound read-write with every XDG base dir pinned to the container
    # home so a host XDG_* cannot redirect it onto the read-only real home.
    # opencode mkdirs all four at startup, so missing any one is a hard crash:
    # EROFS: read-only file system, mkdir '/container_home/.local/state/opencode'
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    fake_apptainer = fakebin / "apptainer"
    fake_apptainer.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\"\n")
    fake_apptainer.chmod(0o755)
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    fake_sif = tmp_path / "ai_tool.sif"
    fake_sif.write_text("")
    env = {
        **os.environ,
        "PATH": f"{fakebin}:{os.environ['PATH']}",
        "HOME": str(fake_home),
        "AI_TOOL_SIF": str(fake_sif),
        # A host XDG_* must not leak through to opencode.
        "XDG_CONFIG_HOME": str(fake_home / "xdg_config"),
        "XDG_STATE_HOME": str(fake_home / "xdg_state"),
        "XDG_CACHE_HOME": str(fake_home / "xdg_cache"),
    }

    p = subprocess.run(
        [str(MQYOLO), "--no-broker", "opencode"],
        text=True,
        capture_output=True,
        env=env,
        cwd=str(fake_home),
    )
    assert p.returncode == 0, p.stderr
    out = p.stdout + p.stderr
    assert "--auto" in out.splitlines()
    assert f"{fake_home}/.config/opencode:/container_home/.config/opencode:rw" in out
    assert f"{fake_home}/.local/share/opencode:/container_home/.local/share/opencode:rw" in out
    assert f"{fake_home}/.local/state/opencode:/container_home/.local/state/opencode:rw" in out
    assert f"{fake_home}/.cache/opencode:/container_home/.cache/opencode:rw" in out
    assert "XDG_CONFIG_HOME=/container_home/.config" in out
    assert "XDG_DATA_HOME=/container_home/.local/share" in out
    assert "XDG_STATE_HOME=/container_home/.local/state" in out
    assert "XDG_CACHE_HOME=/container_home/.cache" in out


def test_mqyolo_opencode_xdg_survives_user_bashrc(tmp_path):
    # An apptainer --env value is applied BEFORE the container sources the user's
    # real ~/.bashrc, so a bashrc that exports XDG_CONFIG_HOME would win and send
    # opencode's config/auth back onto the read-only real home. The shim bashrc
    # must re-assert the pins after sourcing the real one (same fix as PATH).
    real = tmp_path / "real_bashrc"
    real.write_text(
        'export XDG_CONFIG_HOME="$HOME/decoy_config"\n'
        'export XDG_DATA_HOME="$HOME/decoy_data"\n'
        'export XDG_STATE_HOME="$HOME/decoy_state"\n'
        'export XDG_CACHE_HOME="$HOME/decoy_cache"\n'
    )
    dest = tmp_path / "dest_bashrc"
    script = (
        "set -euo pipefail; source %s; "
        "sandbox_write_shim_bashrc %s %s /shims "
        "XDG_CONFIG_HOME=/container_home/.config "
        "XDG_DATA_HOME=/container_home/.local/share "
        "XDG_STATE_HOME=/container_home/.local/state "
        "XDG_CACHE_HOME=/container_home/.cache; "
        "HOME=/container_home; source %s; "
        'printf "%%s\\n%%s\\n%%s\\n%%s\\n" "$XDG_CONFIG_HOME" "$XDG_DATA_HOME" '
        '"$XDG_STATE_HOME" "$XDG_CACHE_HOME"'
        % (SANDBOX_LIB, shlex.quote(str(dest)), shlex.quote(str(real)),
           shlex.quote(str(dest)))
    )
    p = subprocess.run(["bash", "-c", script], text=True, capture_output=True)
    assert p.returncode == 0, p.stderr
    assert p.stdout.split() == [
        "/container_home/.config",
        "/container_home/.local/share",
        "/container_home/.local/state",
        "/container_home/.cache",
    ], p.stdout


def test_mqyolo_does_not_forward_general_aws_credentials(tmp_path):
    # Bedrock access must not drag the user's whole AWS identity into the
    # sandbox: only the Bedrock-scoped API key and the (non-secret) region are
    # forwarded. AWS_PROFILE / access keys are deliberately withheld.
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    fake_apptainer = fakebin / "apptainer"
    fake_apptainer.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\"\n")
    fake_apptainer.chmod(0o755)
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    fake_sif = tmp_path / "ai_tool.sif"
    fake_sif.write_text("")
    env = {
        **os.environ,
        "PATH": f"{fakebin}:{os.environ['PATH']}",
        "HOME": str(fake_home),
        "AI_TOOL_SIF": str(fake_sif),
        "AWS_PROFILE": "sso-profile",
        "AWS_ACCESS_KEY_ID": "AKIAsecret",
        "AWS_SECRET_ACCESS_KEY": "shhh",
        "AWS_SESSION_TOKEN": "sso-session-token",
        "AWS_REGION": "us-east-1",
        "AWS_BEARER_TOKEN_BEDROCK": "bedrock-scoped-key",
    }
    p = subprocess.run(
        [str(MQYOLO), "--no-broker", "opencode"],
        text=True, capture_output=True, env=env, cwd=str(fake_home),
    )
    assert p.returncode == 0, p.stderr
    forwarded = [l for l in (p.stdout + p.stderr).splitlines() if l.startswith("AWS_")]
    assert "AWS_REGION=us-east-1" in forwarded
    assert "AWS_BEARER_TOKEN_BEDROCK=bedrock-scoped-key" in forwarded
    for withheld in ("AWS_PROFILE", "AWS_ACCESS_KEY_ID",
                     "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        assert not any(l.startswith(f"{withheld}=") for l in forwarded), forwarded


def test_mqyolo_rejects_disallowed_launch_dir(tmp_path):
    # The working directory is bound read-write into the sandbox, so mqyolo only
    # allows launching from /work/microbiome, $HOME, /scratch/microbiome/$USER or
    # /tmp. A directory outside those (here "/") is refused up front, before the
    # container is even built.
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    env = {**os.environ, "HOME": str(fake_home), "AI_TOOL_SIF": "/nonexistent.sif"}
    p = subprocess.run(
        [str(MQYOLO), "--no-broker"],
        text=True, capture_output=True, env=env, cwd="/",
    )
    assert p.returncode == 1, (p.returncode, p.stdout, p.stderr)
    assert "must be launched from within" in p.stderr


def test_mqyolo_allows_home_launch_dir(tmp_path):
    # Launching from within $HOME passes the directory check. It may still fail
    # afterwards for unrelated reasons (here, a missing image), but it must NOT be
    # rejected with the launch-directory error.
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    env = {**os.environ, "HOME": str(fake_home), "AI_TOOL_SIF": "/nonexistent.sif"}
    p = subprocess.run(
        [str(MQYOLO), "--no-broker"],
        text=True, capture_output=True, env=env, cwd=str(fake_home),
    )
    assert "must be launched from within" not in p.stderr


# ---------------------------------------------------------------------------
# Broker <-> stub round-trip helpers
# ---------------------------------------------------------------------------
@contextlib.contextmanager
def running_broker(rw_paths=(), ro_paths=(), watch_pid=None, interval=1,
                   bedrock=None, broker_path=None):
    """Start a broker (watching a throwaway parent unless watch_pid given),
    yield (spool_dir, shim_dir, broker_proc, dummy_proc). Cleans up on exit.

    bedrock: (profile, aws_dir) to pass as --bedrock-profile/--bedrock-aws-dir.
    broker_path: run a copy of the broker from elsewhere (used to give it a
    SCRIPT_DIR with a fake mqbedrock in it)."""
    spool = tempfile.mkdtemp(prefix="mqbroker_spool_")
    shim = tempfile.mkdtemp(prefix="mqbroker_shim_")

    dummy = None
    if watch_pid is None:
        dummy = subprocess.Popen(["sleep", "120"])
        watch_pid = dummy.pid

    args = [str(broker_path or BROKER), "--spool", spool,
            "--watch-pid", str(watch_pid), "--watch-interval", str(interval)]
    for p in rw_paths:
        args += ["--rw-path", p]
    for p in ro_paths:
        args += ["--ro-path", p]
    if bedrock:
        args += ["--bedrock-profile", bedrock[0], "--bedrock-aws-dir", str(bedrock[1])]
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


# `snakemake --profile aqua` drives the queue from inside the container via
# snakemake_mqstat (which shells out to `qstat`) and a `qdel` cluster-cancel, so
# the broker must allow both (mqsub/mqstat/mqwait/mqdel cover the rest).
@pytest.mark.skipif(shutil.which("qstat") is None, reason="qstat not on PATH")
def test_broker_allows_qstat():
    with running_broker() as (spool, shim, *_):
        qstat = _stub_as(shim, "qstat")
        # A bogus job id: real qstat runs and errors, but the broker must NOT
        # reject it as non-allowlisted (which would be rc 126 / "not permitted").
        rc, out = _run_stub(qstat, spool, "-x", "-f", "0.nonexistent-mqyolo-test")
        assert "not permitted" not in out, out
        assert not (rc == 126 and "not permitted" in out)


@pytest.mark.skipif(shutil.which("qdel") is None, reason="qdel not on PATH")
def test_broker_allows_qdel():
    with running_broker() as (spool, shim, *_):
        qdel = _stub_as(shim, "qdel")
        rc, out = _run_stub(qdel, spool, "0.nonexistent-mqyolo-test")
        assert "not permitted" not in out, out


def test_broker_propagates_nonzero_exit_and_stderr():
    with running_broker() as (spool, shim, *_):
        mqsub = _stub_as(shim, "mqsub")
        # mqsub with no command errors out with a non-zero exit.
        rc, out = _run_stub(mqsub, spool, "--dry-run")
        assert rc != 0
        assert "Must specify" in out


def test_stub_does_not_block_on_an_idle_stdin_pipe(tmp_path):
    # Claude Code's awsAuthRefresh hook spawns its command with a socket on stdin
    # that it never writes to and never closes. A stub that reads stdin
    # unconditionally blocks there forever: the request is never even renamed into
    # the spool, so the broker never runs it and the AI just watches an empty
    # "Authentication" panel until Claude kills the hook 3 minutes later. Only the
    # commands that can consume stdin may read it.
    with running_broker() as (spool, shim, *_):
        mqstat = _stub_as(shim, "mqstat")
        outfile = tmp_path / "out"
        with open(outfile, "wb") as fh:
            p = subprocess.Popen(
                [mqstat, "--help"], stdin=subprocess.PIPE, stdout=fh,
                stderr=subprocess.STDOUT,
                env={**os.environ, "MQBROKER_SPOOL": spool},
            )
            try:
                deadline = time.time() + 60
                while time.time() < deadline and p.poll() is None:
                    time.sleep(0.2)
                assert p.poll() is not None, \
                    "stub blocked reading an idle stdin pipe"
            finally:
                if p.poll() is None:
                    p.kill()
                p.stdin.close()
                p.wait(timeout=5)


def test_stub_still_forwards_stdin_for_mqsub_script_stdin():
    # The flip side: `mqsub --script -` really does read the job script from
    # stdin, so that one must still be captured and handed to the host.
    with running_broker() as (spool, shim, *_):
        mqsub = _stub_as(shim, "mqsub")
        p = subprocess.run(
            [mqsub, "--dry-run", "--script", "-", "-t", "1", "--hours", "1"],
            input="echo hello-from-stdin\n", text=True, capture_output=True,
            env={**os.environ, "MQBROKER_SPOOL": spool}, timeout=120,
        )
        out = p.stdout + p.stderr
        assert "Wrote 1 lines of stdin" in out, out
        # --dry-run leaves the script it wrote behind (the rm lives in the job
        # script that never gets submitted); don't litter the shared tmpdir.
        for tf in re.findall(r"\S+/mqsub_stdin_\S+\.sh", out):
            with contextlib.suppress(OSError):
                os.unlink(tf)


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


def test_shim_bashrc_reprepends_container_tool_dirs_before_user_paths(tmp_path):
    prefix = "/container_home/.mqyolo/tools:/usr/local/bin:/root/.local/bin"
    real = tmp_path / "real_bashrc"
    real.write_text('export PATH="/container_home/bin:/container_home/.local/bin:$PATH"\n')
    dest = tmp_path / "dest_bashrc"
    script = (
        'source %s; '
        'sandbox_write_shim_bashrc %s %s %s; '
        'PATH=/usr/bin:/bin; source %s; '
        'printf "%%s\\n" "$PATH"'
        % (SANDBOX_LIB, str(dest), str(real), prefix, str(dest))
    )
    p = subprocess.run(["bash", "-c", script], text=True, capture_output=True)
    assert p.returncode == 0, p.stderr
    assert p.stdout.strip().startswith(prefix + ":/container_home/bin"), p.stdout


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


def test_snakemake_cluster_tools_present_in_repo():
    # The aqua/mqsub/lyra snakemake profiles call these; mqyolo stages them onto
    # PATH inside the container, so they must exist in the repo bin.
    assert (BIN / "snakemake_mqsub").exists(), "snakemake_mqsub missing from repo bin"
    assert (BIN / "snakemake_mqstat").exists(), "snakemake_mqstat missing from repo bin"


@pytest.mark.parametrize("tool", ["snakemake_mqsub", "snakemake_mqstat"])
def test_stage_repo_tools_symlinks_repo_tools(tmp_path, tool):
    # So `snakemake --profile aqua` finds the shipped helpers. (pixi and the mqpixi
    # env wrapper ship only in the deployed bin/, so they are asserted separately as
    # "declared" rather than symlinked from a dev checkout.)
    tools = tmp_path / "tools"
    script = (
        "source %s; sandbox_stage_repo_tools %s %s; readlink -f %s/%s"
        % (SANDBOX_LIB, shlex.quote(str(tools)), shlex.quote(str(BIN)),
           shlex.quote(str(tools)), shlex.quote(tool))
    )
    p = subprocess.run(["bash", "-c", script], text=True, capture_output=True)
    assert p.returncode == 0, p.stderr
    assert p.stdout.strip() == os.path.realpath(str(BIN / tool)), p.stdout


def test_stage_repo_tools_survives_missing_tool(tmp_path):
    # Regression: a declared-but-absent tool (e.g. the deployed-only pixi/mqpixi, or
    # a renamed file) must NOT make sandbox_stage_repo_tools return non-zero. It is
    # called as a bare command under mqyolo's `set -e`, so a non-zero return there
    # aborts mqyolo before it launches. Simulate by pointing staging at an empty dir
    # under `set -e` — nothing gets staged, but it must still succeed.
    tools = tmp_path / "tools"
    empty = tmp_path / "empty_bin"
    empty.mkdir()
    script = (
        "set -euo pipefail; source %s; sandbox_stage_repo_tools %s %s; echo OK"
        % (shlex.quote(str(SANDBOX_LIB)), shlex.quote(str(tools)), shlex.quote(str(empty)))
    )
    p = subprocess.run(["bash", "-c", script], text=True, capture_output=True)
    assert p.returncode == 0, p.stderr
    assert p.stdout.strip() == "OK", p.stdout


@pytest.mark.parametrize(
    "subdirs, expect_rw",
    [
        (["scratch"], ["scratch"]),
        (["tmp"], ["tmp"]),
        (["scratch", "tmp"], ["scratch", "tmp"]),
        ([], []),
        (["other"], []),  # only the declared subdirs are made writable
    ],
)
def test_add_default_scratch_paths_mounts_writable_subdirs(tmp_path, subdirs, expect_rw):
    # sandbox_add_default_scratch_paths exposes the non_sensitive tree read-only and
    # each existing SANDBOX_SCRATCH_RW_SUBDIRS entry (scratch, tmp) read-write. Drive
    # it with an override base dir so we don't touch the real /scratch tree.
    ns = tmp_path / "non_sensitive"
    ns.mkdir()
    for d in subdirs:
        (ns / d).mkdir()
    script = (
        "set -euo pipefail; source %s; RO_PATHS=(); RW_PATHS=(); "
        "sandbox_add_default_scratch_paths %s; "
        'printf "RO:%%s\\n" "${RO_PATHS[@]:-}"; printf "RW:%%s\\n" "${RW_PATHS[@]:-}"'
        % (shlex.quote(str(SANDBOX_LIB)), shlex.quote(str(ns)))
    )
    p = subprocess.run(["bash", "-c", script], text=True, capture_output=True)
    assert p.returncode == 0, p.stderr
    ro = [l[len("RO:"):] for l in p.stdout.splitlines() if l.startswith("RO:") and l != "RO:"]
    rw = [l[len("RW:"):] for l in p.stdout.splitlines() if l.startswith("RW:") and l != "RW:"]
    assert ro == [str(ns)], p.stdout
    assert rw == [str(ns / d) for d in expect_rw], p.stdout


# ---------------------------------------------------------------------------
# NVIDIA GPU passthrough (sandbox_add_gpu_args)
# ---------------------------------------------------------------------------
def _add_gpu_args(globs, env=None):
    """Run sandbox_add_gpu_args with SANDBOX_GPU_DEVICE_GLOBS overridden, and
    return (GPU_ARGS, [bind specs])."""
    script = (
        "set -euo pipefail; source %s; "
        "SANDBOX_GPU_DEVICE_GLOBS=(%s); BIND_ARGS=(); GPU_ARGS=(); "
        "sandbox_add_gpu_args; "
        'printf "GPU:%%s\\n" "${GPU_ARGS[@]:-}"; printf "BIND:%%s\\n" "${BIND_ARGS[@]:-}"'
        % (shlex.quote(str(SANDBOX_LIB)), " ".join(shlex.quote(g) for g in globs))
    )
    p = subprocess.run(["bash", "-c", script], text=True, capture_output=True,
                       env={**os.environ, **(env or {})})
    assert p.returncode == 0, p.stderr
    gpu = [l[len("GPU:"):] for l in p.stdout.splitlines()
           if l.startswith("GPU:") and l != "GPU:"]
    binds = [l[len("BIND:"):] for l in p.stdout.splitlines()
             if l.startswith("BIND:") and l not in ("BIND:", "BIND:--bind")]
    return gpu, binds


def _fake_nvidia_devices(tmp_path, names):
    dev = tmp_path / "dev"
    dev.mkdir(exist_ok=True)
    for n in names:
        (dev / n).write_text("")
    return dev


def test_add_gpu_args_requests_nv_when_a_device_node_exists(tmp_path):
    # A device node means the host has a driver loaded, so the container needs --nv
    # to reach the driver userspace libs (libcuda.so et al). Without it CUDA reports
    # cudaErrorInsufficientDriver even though PBS allocated a real GPU.
    dev = _fake_nvidia_devices(tmp_path, ["nvidia0", "nvidia1", "nvidiactl", "nvidia-uvm"])
    gpu, binds = _add_gpu_args([f"{dev}/nvidia[0-9]*", f"{dev}/nvidiactl",
                                f"{dev}/nvidia-uvm", f"{dev}/nvidia-absent"])
    assert gpu == ["--nv"]
    # The globs are for DETECTION ONLY. Binding the device nodes ourselves is
    # redundant -- the runtime's --nv already binds them (a superset: a real A100
    # job also got /dev/nvidia-nvlink and /dev/nvidia-nvswitch0-5) -- and earns a
    # "Skipping ... bind mount: already mounted" warning per device on every GPU
    # job, which lands in every snakemake log.
    assert binds == []


def test_add_gpu_args_detects_a_driver_from_any_single_glob(tmp_path):
    # Detection must not depend on which device node happens to exist: a host with
    # only /dev/nvidiactl (no numbered GPU visible yet) still has a driver.
    dev = _fake_nvidia_devices(tmp_path, ["nvidiactl"])
    gpu, _ = _add_gpu_args([f"{dev}/nvidia[0-9]*", f"{dev}/nvidiactl"])
    assert gpu == ["--nv"]


def test_add_gpu_args_is_a_noop_without_a_driver(tmp_path):
    # A CPU-only node (login node, non-GPU compute node) must be untouched: no
    # --nv (which would warn about missing nv files) and no extra binds.
    dev = _fake_nvidia_devices(tmp_path, [])
    gpu, binds = _add_gpu_args([f"{dev}/nvidia[0-9]*", f"{dev}/nvidiactl"])
    assert gpu == []
    assert binds == []


@pytest.mark.parametrize("value, expect_nv", [("0", False), ("1", True)])
def test_add_gpu_args_honours_mqsandbox_nv_override(tmp_path, value, expect_nv):
    # MQSANDBOX_NV forces the decision either way, against what detection sees:
    # devices present + MQSANDBOX_NV=0 -> off; no devices + MQSANDBOX_NV=1 -> on.
    names = ["nvidia0"] if value == "0" else []
    dev = _fake_nvidia_devices(tmp_path, names)
    gpu, binds = _add_gpu_args([f"{dev}/nvidia[0-9]*"], env={"MQSANDBOX_NV": value})
    assert gpu == (["--nv"] if expect_nv else [])
    assert binds == []


def test_mqsub_sandbox_wrapper_defers_the_gpu_decision_to_the_exec_node():
    # The whole point: GPU passthrough must NOT be gated on the submitting node.
    # mqsub runs on a CPU-only login node, so it bakes NO GPU flag into the job
    # script -- only --cwd/--rw-paths/--ro-paths. mqsandbox then detects the driver
    # on the compute node it lands on, so an --A100/--H100 job gets the GPU even
    # though nothing GPU-ish existed where it was submitted from.
    rc, out = _mqsub_dry_run("--sandbox", "--A100", "--", "python", "train.py")
    assert rc == 0, out
    assert "mqsandbox" in out
    assert "ngpus=1" in out          # the PBS request IS made at submit time
    assert "gpu_id=A100" in out
    # ...but the sandbox invocation carries no GPU decision of its own.
    wrapper = [l for l in out.splitlines() if "mqsandbox" in l][0]
    assert "--nv" not in wrapper, wrapper
    assert "/dev/nvidia" not in wrapper, wrapper


def test_mqsub_forwards_nv_override_across_the_submit_boundary(monkeypatch):
    # mqsub does not `qsub -V`, so the job runs with PBS's own environment. The
    # MQSANDBOX_NV escape hatch would therefore never reach mqsandbox on the exec
    # node unless mqsub forwards it -- leaving no way to force passthrough on (or
    # off) for a job submitted from a CPU-only node.
    monkeypatch.setenv("MQSANDBOX_NV", "1")
    rc, out = _mqsub_dry_run("--sandbox", "--A100", "--", "python", "train.py")
    assert rc == 0, out
    wrapper = [l for l in out.splitlines() if "mqsandbox" in l][0]
    assert wrapper.strip().startswith("MQSANDBOX_NV=1 "), wrapper


def test_mqsub_sandbox_wrapper_omits_nv_override_when_unset(monkeypatch):
    # No env prefix when the escape hatch is not in use, so the ordinary job script
    # stays exactly as it was.
    monkeypatch.delenv("MQSANDBOX_NV", raising=False)
    rc, out = _mqsub_dry_run("--sandbox", "--", "echo", "hi")
    assert rc == 0, out
    wrapper = [l for l in out.splitlines() if "mqsandbox" in l][0]
    assert "MQSANDBOX_NV" not in wrapper, wrapper


def _fake_runtime_argv(tmp_path, argv, env=None, cwd=None):
    """Run mqyolo/mqsandbox against a fake apptainer that echoes its arguments."""
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir(exist_ok=True)
    fake_apptainer = fakebin / "apptainer"
    fake_apptainer.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\"\n")
    fake_apptainer.chmod(0o755)
    fake_home = tmp_path / "home"
    fake_home.mkdir(exist_ok=True)
    fake_sif = tmp_path / "ai_tool.sif"
    fake_sif.write_text("")
    p = subprocess.run(
        argv, text=True, capture_output=True,
        env={**os.environ, "PATH": f"{fakebin}:{os.environ['PATH']}",
             "HOME": str(fake_home), "AI_TOOL_SIF": str(fake_sif), **(env or {})},
        cwd=str(cwd or fake_home),
    )
    assert p.returncode == 0, p.stderr
    return p.stdout + p.stderr


@pytest.mark.parametrize("nv, expect_nv", [("1", True), ("0", False)])
def test_mqsandbox_passes_nv_to_the_runtime(tmp_path, nv, expect_nv):
    # mqsandbox is what `mqsub --sandbox` wraps every submitted job in, so this is
    # where the GPU flag has to land for a queued CUDA job to see a driver.
    out = _fake_runtime_argv(tmp_path, [str(MQSANDBOX), "--", "true"],
                             env={"MQSANDBOX_NV": nv})
    assert ("--nv" in out.splitlines()) is expect_nv, out


@pytest.mark.parametrize("nv, expect_nv", [("1", True), ("0", False)])
def test_mqyolo_passes_nv_to_the_runtime(tmp_path, nv, expect_nv):
    # The interactive session gets the same treatment, for mqyolo run inside an
    # interactive PBS GPU job or on a GPU workstation.
    out = _fake_runtime_argv(tmp_path, [str(MQYOLO), "--no-broker", "claude"],
                             env={"MQSANDBOX_NV": nv})
    assert ("--nv" in out.splitlines()) is expect_nv, out


def test_mqsandbox_forwards_cuda_visible_devices(tmp_path):
    # PBS sets CUDA_VISIBLE_DEVICES in the job environment to the GPU(s) it
    # allocated; the job inside the sandbox must still see it.
    out = _fake_runtime_argv(tmp_path, [str(MQSANDBOX), "--", "true"],
                             env={"CUDA_VISIBLE_DEVICES": "GPU-deadbeef"})
    assert "CUDA_VISIBLE_DEVICES=GPU-deadbeef" in out


def test_pixi_and_mqpixi_declared_as_repo_tools():
    # pixi (the package manager) and mqpixi (its CMR wrapper) must be staged onto
    # PATH inside the container so the in-container AI can build/run pixi envs.
    # Both ship only in the deployed bin/, so staging is existence-guarded and we
    # only assert they are declared, not present in the repo.
    script = (
        'source %s; printf "%%s\\n" "${SANDBOX_REPO_TOOLS[@]}"' % shlex.quote(str(SANDBOX_LIB))
    )
    p = subprocess.run(["bash", "-c", script], text=True, capture_output=True)
    assert p.returncode == 0, p.stderr
    declared = p.stdout.split()
    assert "pixi" in declared, declared
    assert "mqpixi" in declared, declared


def test_stage_repo_tools_stages_pixi_when_present(tmp_path):
    # When pixi is present in the bin dir mqyolo runs from (the deployed copy),
    # sandbox_stage_repo_tools symlinks it into the tools dir at the shipped target.
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    pixi = fake_bin / "pixi"
    pixi.write_text("#!/bin/sh\necho pixi\n")
    pixi.chmod(0o755)
    tools = tmp_path / "tools"
    script = (
        "source %s; sandbox_stage_repo_tools %s %s; readlink -f %s/pixi"
        % (SANDBOX_LIB, shlex.quote(str(tools)), shlex.quote(str(fake_bin)),
           shlex.quote(str(tools)))
    )
    p = subprocess.run(["bash", "-c", script], text=True, capture_output=True)
    assert p.returncode == 0, p.stderr
    assert p.stdout.strip() == os.path.realpath(str(pixi)), p.stdout


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


def test_bind_codex_home_mounts_real_dir_rw_and_guidance_readonly(tmp_path):
    real = tmp_path / "real_codex"
    guidance = tmp_path / "guidance.md"
    guidance.write_text("use mqsub\n")

    script = (
        "set -euo pipefail\n"
        "source %s; "
        "BIND_ARGS=(); "
        "sandbox_bind_codex_home %s /container_home/.codex %s; "
        'for a in "${BIND_ARGS[@]}"; do [[ "$a" == --bind ]] || printf "%%s\\n" "$a"; done'
        % (
            SANDBOX_LIB,
            shlex.quote(str(real)),
            shlex.quote(str(guidance)),
        )
    )
    p = subprocess.run(["bash", "-c", script], text=True, capture_output=True)
    assert p.returncode == 0, p.stderr
    assert real.is_dir()
    binds = p.stdout.splitlines()
    assert f"{real}:/container_home/.codex:rw" in binds
    assert f"{guidance}:/container_home/.codex/AGENTS.md:ro" in binds


def _home_dotfiles(home, opt_ins=()):
    """Drive sandbox_home_dotfiles against a fake HOME. Returns
    (bind specs, container-home entry names)."""
    args = " ".join(shlex.quote(str(p)) for p in opt_ins)
    script = (
        "set -euo pipefail\n"
        "source %s\n"
        "HOME=%s\n"
        "CONTAINER_HOME=$(mktemp -d)\n"
        "BIND_ARGS=()\n"
        "sandbox_home_dotfiles %s\n"
        'for a in "${BIND_ARGS[@]}"; do [[ "$a" == --bind ]] || printf "bind %%s\\n" "$a"; done\n'
        'for e in "$CONTAINER_HOME"/.*; do printf "entry %%s\\n" "${e##*/}"; done\n'
        'rm -rf "$CONTAINER_HOME"\n'
        % (SANDBOX_LIB, shlex.quote(str(home)), args)
    )
    p = subprocess.run(["bash", "-c", script], text=True, capture_output=True)
    assert p.returncode == 0, p.stderr
    binds = [l[5:] for l in p.stdout.splitlines() if l.startswith("bind ")]
    entries = [l[6:] for l in p.stdout.splitlines() if l.startswith("entry ")]
    return binds, entries


def test_home_dotfiles_shadows_aws_credentials_by_default(tmp_path):
    # ~/.aws/sso/cache holds an SSO access token exchangeable for role
    # credentials across every account the user can reach — exposing it
    # read-only still hands the untrusted in-container tool the caller's whole
    # AWS identity. It must be shadowed like ~/.ssh, not symlinked through.
    home = tmp_path / "home"
    (home / ".aws" / "sso" / "cache").mkdir(parents=True)
    (home / ".aws" / "sso" / "cache" / "tok.json").write_text('{"accessToken":"x"}')
    (home / ".ssh").mkdir()
    (home / ".gitconfig").write_text("[user]\n")

    binds, entries = _home_dotfiles(home)

    aws_real = os.path.realpath(home / ".aws")
    shadow = [b for b in binds if b.endswith(f":{aws_real}:ro")]
    assert len(shadow) == 1, binds
    assert "_empty_aws" in shadow[0], shadow
    # No symlink into the real ~/.aws, so it isn't reachable via the home bind.
    assert ".aws" not in entries, entries
    # Unrelated dotfiles are still linked through.
    assert ".gitconfig" in entries, entries


def test_home_dotfiles_aws_can_be_opted_back_in(tmp_path):
    # An explicit --ro-paths ~/.aws is a deliberate choice — honour it. The
    # shadow bind is appended after the caller's ro-path bind and dedupe keeps
    # the last one, so the shadow must be skipped rather than layered on top.
    home = tmp_path / "home"
    (home / ".aws").mkdir(parents=True)

    binds, entries = _home_dotfiles(home, opt_ins=[home / ".aws"])

    aws_real = os.path.realpath(home / ".aws")
    assert not any(b.endswith(f":{aws_real}:ro") for b in binds), binds
    # The symlink must exist, or ~/.aws is unreachable at the container home.
    assert ".aws" in entries, entries


def test_home_dotfiles_ssh_shadow_is_not_opt_innable(tmp_path):
    # Private keys have no legitimate in-sandbox use; unlike ~/.aws there is no
    # escape hatch, so passing ~/.ssh as a granted path must not expose it.
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)

    binds, entries = _home_dotfiles(home, opt_ins=[home / ".ssh"])

    ssh_real = os.path.realpath(home / ".ssh")
    shadow = [b for b in binds if b.endswith(f":{ssh_real}:ro")]
    assert len(shadow) == 1 and "_empty_ssh" in shadow[0], binds
    assert ".ssh" not in entries, entries


def test_mirror_home_subdir_replaces_symlink_with_dir_of_symlinks(tmp_path):
    # sandbox_home_dotfiles leaves ~/.config as a symlink onto the (read-only)
    # real home. Mirroring must replace the LINK — never write through it — with a
    # real dir of symlinks, minus the skipped entry, so a bind can be mounted
    # inside it.
    home = tmp_path / "home"
    (home / ".config" / "git").mkdir(parents=True)
    (home / ".config" / "opencode").mkdir()
    chome = tmp_path / "chome"
    chome.mkdir()
    (chome / ".config").symlink_to(home / ".config")

    script = (
        "set -euo pipefail\n"
        "source %s; "
        "HOME=%s CONTAINER_HOME=%s; "
        "sandbox_mirror_home_subdir .config opencode"
        % (SANDBOX_LIB, shlex.quote(str(home)), shlex.quote(str(chome)))
    )
    p = subprocess.run(["bash", "-c", script], text=True, capture_output=True)
    assert p.returncode == 0, p.stderr

    mirrored = chome / ".config"
    assert mirrored.is_dir() and not mirrored.is_symlink()
    assert (mirrored / "git").is_symlink()
    assert os.path.realpath(mirrored / "git") == os.path.realpath(home / ".config" / "git")
    # The entry taken over by a bind must NOT be symlinked, or the bind
    # destination would resolve back onto the read-only real home.
    assert not (mirrored / "opencode").exists()
    # The real home is untouched.
    assert (home / ".config" / "git").is_dir()
    assert (home / ".config" / "opencode").is_dir()


def test_bind_opencode_home_mounts_real_dirs_rw_and_guidance_readonly(tmp_path):
    home = tmp_path / "home"
    (home / ".config").mkdir(parents=True)
    (home / ".local" / "share").mkdir(parents=True)
    (home / ".local" / "state").mkdir(parents=True)
    chome = tmp_path / "chome"
    chome.mkdir()
    # As sandbox_home_dotfiles leaves them: symlinks onto the real home, except
    # ~/.cache which it builds as a real directory.
    (chome / ".config").symlink_to(home / ".config")
    (chome / ".local").symlink_to(home / ".local")
    (chome / ".cache").mkdir()
    config = home / ".config" / "opencode"
    data = home / ".local" / "share" / "opencode"
    state = home / ".local" / "state" / "opencode"
    cache = home / ".cache" / "opencode"
    guidance = tmp_path / "guidance.md"
    guidance.write_text("use mqsub\n")

    script = (
        "set -euo pipefail\n"
        "source %s; "
        "HOME=%s CONTAINER_HOME=%s; "
        "BIND_ARGS=(); "
        "sandbox_bind_opencode_home %s %s %s %s %s; "
        'for a in "${BIND_ARGS[@]}"; do [[ "$a" == --bind ]] || printf "%%s\\n" "$a"; done'
        % (
            SANDBOX_LIB,
            shlex.quote(str(home)),
            shlex.quote(str(chome)),
            shlex.quote(str(config)),
            shlex.quote(str(data)),
            shlex.quote(str(state)),
            shlex.quote(str(cache)),
            shlex.quote(str(guidance)),
        )
    )
    p = subprocess.run(["bash", "-c", script], text=True, capture_output=True)
    assert p.returncode == 0, p.stderr
    # All four state dirs are created in the real home so the binds have a source.
    assert config.is_dir() and data.is_dir() and state.is_dir() and cache.is_dir()
    binds = p.stdout.splitlines()
    assert f"{config}:/container_home/.config/opencode:rw" in binds
    assert f"{data}:/container_home/.local/share/opencode:rw" in binds
    assert f"{state}:/container_home/.local/state/opencode:rw" in binds
    assert f"{cache}:/container_home/.cache/opencode:rw" in binds
    assert f"{guidance}:/container_home/.config/opencode/AGENTS.md:ro" in binds
    # The XDG parents are now real dirs inside the ephemeral home, with the
    # opencode mountpoints present and NOT symlinked at the real home.
    for rel in (".config", ".local", ".local/share", ".local/state", ".cache"):
        assert (chome / rel).is_dir() and not (chome / rel).is_symlink()
    for rel in (".config/opencode", ".local/share/opencode",
                ".local/state/opencode", ".cache/opencode"):
        assert (chome / rel).is_dir(), rel
        assert not (chome / rel).is_symlink(), rel


def test_bind_opencode_home_without_guidance_omits_agents_bind(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    chome = tmp_path / "chome"
    chome.mkdir()
    script = (
        "set -euo pipefail\n"
        "source %s; "
        "HOME=%s CONTAINER_HOME=%s; "
        "BIND_ARGS=(); "
        "sandbox_bind_opencode_home %s %s %s %s %s; "
        'for a in "${BIND_ARGS[@]}"; do [[ "$a" == --bind ]] || printf "%%s\\n" "$a"; done'
        % (
            SANDBOX_LIB,
            shlex.quote(str(home)),
            shlex.quote(str(chome)),
            shlex.quote(str(home / ".config" / "opencode")),
            shlex.quote(str(home / ".local" / "share" / "opencode")),
            shlex.quote(str(home / ".local" / "state" / "opencode")),
            shlex.quote(str(home / ".cache" / "opencode")),
            shlex.quote(str(tmp_path / "missing_guidance.md")),
        )
    )
    p = subprocess.run(["bash", "-c", script], text=True, capture_output=True)
    assert p.returncode == 0, p.stderr
    assert "AGENTS.md" not in p.stdout


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


def _build_binds(cwd, rw_paths=(), ro_paths=(), deny_mounts=None, wholesale_dirs=None):
    """Drive sandbox_build_binds and return the list of `src:dst:mode` bind specs.

    `deny_mounts`, when given, overrides the sshfs mounts sandbox_build_binds
    would deny (normally auto-discovered from /proc/mounts) so the remote-mount
    denial can be exercised without a real sshfs mount. `wholesale_dirs`, when
    given, overrides the fixed top-level dirs bound read-only (SANDBOX_WHOLESALE_BIND_DIRS)
    so a fake symlinked mount can be exercised without touching real /work etc.
    """
    args = " ".join(shlex.quote(p) for p in rw_paths)
    if ro_paths:
        args += " -- " + " ".join(shlex.quote(p) for p in ro_paths)
    inject = ""
    if deny_mounts is not None:
        inject += "SANDBOX_DENY_MOUNTS=(%s)\n" % " ".join(
            shlex.quote(m) for m in deny_mounts
        )
    if wholesale_dirs is not None:
        inject += "SANDBOX_WHOLESALE_BIND_DIRS=(%s)\n" % " ".join(
            shlex.quote(d) for d in wholesale_dirs
        )
    script = (
        "set -euo pipefail\n"
        "source %s\n"
        "%s"
        "CONTAINER_HOME=$(mktemp -d)\n"
        "BIND_ARGS=()\n"
        "sandbox_build_binds %s %s\n"
        'for a in "${BIND_ARGS[@]}"; do [[ "$a" == --bind ]] || printf "%%s\\n" "$a"; done\n'
        "rm -rf \"$CONTAINER_HOME\"\n"
        % (SANDBOX_LIB, inject, shlex.quote(str(cwd)), args)
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


def test_build_binds_denies_canonical_aliases(tmp_path):
    # The deny-list is written with LOGICAL paths (/scratch, /work/microbiome),
    # but on this HPC the same data is reachable through the canonical mount
    # alias (/scratch -> /mnt/weka/scratch, /work/microbiome ->
    # /mnt/hpccs01/work/microbiome). Home symlinks like ~/s and ~/m are rewritten
    # to those canonical targets, so denying only the logical path used to leak
    # the data read-only. The canonical alias must be denied too: never bound
    # read-only from its real path, and (when nested under an exposed parent like
    # /mnt) shadowed by an empty dir.
    binds = _build_binds(tmp_path)
    for logical in ("/scratch", "/work/microbiome"):
        canonical = os.path.realpath(logical)
        if canonical == logical:
            continue  # alias not present on this machine; nothing to assert
        # The canonical tree is never re-exposed read-only from its real path.
        assert "%s:%s:ro" % (canonical, canonical) not in binds, (canonical, binds)
        # If it sits under a still-exposed parent (e.g. /mnt), it is shadowed by
        # an empty dir bound from the ephemeral CONTAINER_HOME at the same path.
        if canonical.count("/") >= 2:
            shadow = [b for b in binds if b.endswith(":%s:ro" % canonical)]
            assert len(shadow) == 1 and "_denied" in shadow[0], (canonical, binds)


def test_build_binds_ro_paths_are_bound_readonly(tmp_path):
    rodir = tmp_path / "ro_extra"
    rodir.mkdir()
    binds = _build_binds(tmp_path, ro_paths=[str(rodir)])
    real = os.path.realpath(str(rodir))
    assert "%s:%s:ro" % (real, real) in binds, binds


def _realpath_no_symlinks(path):
    """The literal absolute path bash's `realpath -s` produces (symlinks kept)."""
    return subprocess.run(["realpath", "-s", path], text=True,
                          capture_output=True).stdout.strip()


def test_build_binds_ro_path_via_symlink_bound_at_literal_path(tmp_path):
    # On this HPC /scratch is a symlink to /mnt/weka/scratch and that symlink is
    # absent inside the --contain'd container, so a --ro-path given through a
    # symlink must be exposed at BOTH the resolved real path (the bind source) and
    # the literal path the user typed, or it is invisible where they asked for it.
    target = tmp_path / "target" / "sub"
    target.mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "target")
    ro_path = str(link / "sub")

    binds = _build_binds(tmp_path, ro_paths=[ro_path])
    src = os.path.realpath(ro_path)          # bind source: symlinks resolved
    dst = _realpath_no_symlinks(ro_path)     # user-facing dest: symlinks kept
    assert src != dst, "test setup: symlink did not change the path"
    assert "%s:%s:ro" % (src, src) in binds, binds   # resolved location
    assert "%s:%s:ro" % (src, dst) in binds, binds   # literal location


def test_build_binds_rw_path_via_symlink_bound_at_literal_path(tmp_path):
    target = tmp_path / "target" / "sub"
    target.mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "target")
    rw_path = str(link / "sub")

    binds = _build_binds(tmp_path, rw_paths=[rw_path])
    src = os.path.realpath(rw_path)
    dst = _realpath_no_symlinks(rw_path)
    assert src != dst, "test setup: symlink did not change the path"
    assert "%s:%s:rw" % (src, src) in binds, binds
    assert "%s:%s:rw" % (src, dst) in binds, binds


# ---------------------------------------------------------------------------
# Remote FUSE (sshfs) mounts are denied by default. mqyolo/mqsandbox may run on a
# workstation that sshfs-mounts sensitive remote trees (e.g. /work/projects); that
# data must NOT appear in the sandbox — not even read-only — unless a path is
# expressly opted in with --ro-paths/--rw-paths.
# ---------------------------------------------------------------------------
def test_collect_remote_deny_mounts_matches_sshfs(tmp_path):
    # sandbox_collect_remote_deny_mounts picks out sshfs (fuse.sshfs and plain
    # sshfs) mountpoints from a /proc/mounts-format file and ignores everything else.
    fake = tmp_path / "mounts"
    fake.write_text(
        "user@host:/data /work/projects fuse.sshfs rw,nosuid,nodev 0 0\n"
        "/dev/sda1 / ext4 rw 0 0\n"
        "proc /proc proc rw 0 0\n"
        "user@host:/x /mnt/remote sshfs rw 0 0\n"
        "tmpfs /run tmpfs rw 0 0\n"
        "/dev/sdb1 /mnt/data ext4 rw 0 0\n"
    )
    script = (
        "source %s\n"
        "sandbox_collect_remote_deny_mounts %s\n"
        'printf "%%s\\n" "${SANDBOX_DENY_MOUNTS[@]}"\n'
        % (SANDBOX_LIB, shlex.quote(str(fake)))
    )
    p = subprocess.run(["bash", "-c", script], text=True, capture_output=True)
    assert p.returncode == 0, p.stderr
    assert p.stdout.split() == ["/work/projects", "/mnt/remote"], p.stdout


def test_sandbox_path_denied_includes_remote_mounts():
    # A discovered sshfs mount (and everything under it) is denied, while a sibling
    # path that merely shares a parent is not.
    script = (
        "source %s\n"
        "SANDBOX_DENY_MOUNTS=(/work/projects)\n"
        "sandbox_path_denied /work/projects && echo D || echo A\n"
        "sandbox_path_denied /work/projects/secret && echo D || echo A\n"
        "sandbox_path_denied /work/other && echo D || echo A\n"
        % SANDBOX_LIB
    )
    p = subprocess.run(["bash", "-c", script], text=True, capture_output=True)
    assert p.returncode == 0, p.stderr
    assert p.stdout.split() == ["D", "D", "A"], p.stdout


def test_build_binds_denies_remote_fuse_mount(tmp_path):
    # A remote sshfs mount nested under a still-exposed parent (/work) must never be
    # bound read-only from its real path, and must be shadowed by an empty dir so it
    # cannot leak through /work's recursive bind.
    mount = "/work/projects"
    binds = _build_binds(tmp_path, deny_mounts=[mount])
    assert "%s:%s:ro" % (mount, mount) not in binds, binds
    shadow = [b for b in binds if b.endswith(":%s:ro" % mount)]
    assert len(shadow) == 1 and "_denied" in shadow[0], binds


def test_build_binds_remote_fuse_mount_reexposed_via_ro_path(tmp_path):
    # Even though the sshfs mount is denied, an explicitly opted-in sub-path
    # (--ro-paths) is bound read-only on top of the shadow, so the user can still
    # grant access to a specific directory.
    mount = tmp_path / "sshfs_mount"
    sub = mount / "allowed"
    sub.mkdir(parents=True)
    binds = _build_binds(tmp_path, ro_paths=[str(sub)], deny_mounts=[str(mount)])
    shadow = [b for b in binds if b.endswith(":%s:ro" % mount)]
    assert len(shadow) == 1 and "_denied" in shadow[0], binds
    real = os.path.realpath(str(sub))
    assert "%s:%s:ro" % (real, real) in binds, binds


def test_build_binds_skips_wholesale_dir_symlinked_into_denied_mount(tmp_path):
    # Real-world regression (this workstation): /work is a symlink whose target is
    # UNDER an sshfs mount (/work -> /mnt/<sshfs>/.../work). Its literal path is not
    # a mount, so binding it would resolve the symlink and expose the remote tree at
    # /work. A wholesale bind dir whose realpath falls in a denied mount must be
    # skipped entirely (never bound), while a sibling pointing outside it is bound.
    mount = tmp_path / "sshfs_mount"
    (mount / "work").mkdir(parents=True)
    safe = tmp_path / "safe_target"
    safe.mkdir()

    into_mount = tmp_path / "link_into_mount"   # -> denied mount: must be skipped
    into_mount.symlink_to(mount / "work")
    into_safe = tmp_path / "link_safe"          # -> outside: must be bound
    into_safe.symlink_to(safe)

    binds = _build_binds(
        tmp_path,
        deny_mounts=[str(mount)],
        wholesale_dirs=[str(into_mount), str(into_safe)],
    )
    assert not any(b.startswith("%s:" % into_mount) for b in binds), binds
    assert "%s:%s:ro" % (into_safe, into_safe) in binds, binds


# ---------------------------------------------------------------------------
# mqsandbox actually enforcing the filesystem constraints (needs the container)
# ---------------------------------------------------------------------------
def _run_in_sandbox(cwd, script, rw_paths=()):
    args = [str(MQSANDBOX), "--cwd", str(cwd)]
    for p in rw_paths:
        args += ["--rw-paths", p]
    args += ["--", "bash", "-c", script]
    p = subprocess.run(args, text=True, capture_output=True, timeout=120)
    if p.returncode == 255:
        output = p.stdout + p.stderr
        if "socket communication error" in output:
            pytest.skip("container runtime cannot pass sockets in this sandbox")
        if "Couldn't determine user account information" in output:
            pytest.skip("container runtime cannot resolve this sandbox user")
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
    # /work/microbiome must expose ONLY its sw and db sub-paths (the carved-out
    # read-only exceptions) — everything else under it is hidden behind the shadow
    # dir. /scratch is a denied tree too, with ONE carve-out: the per-user
    # non_sensitive default that sandbox_add_default_scratch_paths auto-mounts when
    # it exists on the host. When present, that path (and nothing else under
    # /scratch) is visible — crucially, sibling users' scratch stays hidden; when
    # absent, /scratch does not exist in the sandbox at all.
    user = os.environ.get("USER", "")
    ns_host = "/scratch/microbiome/%s/non_sensitive" % user
    ns_present = bool(user) and os.path.isdir(ns_host)

    cwd = tempfile.mkdtemp(prefix="mqs_cwd_", dir=str(REPO))
    script = (
        'echo -n "scratch:"; ([[ -e /scratch ]] && echo PRESENT || echo ABSENT); echo; '
        'echo -n "ns:"; ([[ -d %s ]] && echo PRESENT || echo ABSENT); echo; '
        'echo -n "scratch-users:"; (ls -1 /scratch/microbiome 2>/dev/null | sort | tr "\\n" ","); echo; '
        'echo -n "microbiome:"; (ls -1 /work/microbiome 2>/dev/null | sort | tr "\\n" ","); echo'
        % shlex.quote(ns_host)
    )
    try:
        rc, out = _run_in_sandbox(cwd, script)
        assert rc == 0, out
        assert "microbiome:db,sw," in out, out
        if ns_present:
            # The default is exposed at its literal path, and /scratch reveals ONLY
            # this user under microbiome — no sibling users leak in.
            assert "ns:PRESENT" in out, out
            assert "scratch-users:%s," % user in out, out
        else:
            # Nothing to carve out, so the whole denied tree stays hidden.
            assert "scratch:ABSENT" in out, out
    finally:
        shutil.rmtree(cwd, ignore_errors=True)


# ---------------------------------------------------------------------------
# Bedrock credentials in the sandbox
#
# ~/.aws is shadowed, so a Bedrock-backed Claude Code (CLAUDE_CODE_USE_BEDROCK +
# AWS_PROFILE, set from ~/.claude/settings.json) finds no credential and the AWS
# SDK falls through to the blackholed instance-metadata address — the session
# starts and then silently never answers. mqyolo stages ONLY that profile's static
# keys into the ephemeral home; everything else in ~/.aws stays invisible.
# ---------------------------------------------------------------------------
MQBEDROCK = BIN / "mqbedrock"

BEDROCK_CREDENTIALS = """\
[default]
aws_access_key_id = AKIA_OTHER_IDENTITY
aws_secret_access_key = other-secret

[bedrock]
aws_access_key_id = AKIA_BEDROCK
aws_secret_access_key = bedrock-secret==
aws_session_token = tok/en+with=padding==
"""

BEDROCK_CONFIG = """\
[profile bedrock-sso]
sso_session = my-sso-bedrock

[profile bedrock]
region = ap-southeast-2
"""


def _fake_aws_home(home, credentials=BEDROCK_CREDENTIALS, config=BEDROCK_CONFIG):
    """Populate home/.aws the way mqbedrock leaves it, including the secrets that
    must NOT reach the sandbox (SSO token, CLI role cache, other profiles)."""
    aws = home / ".aws"
    (aws / "sso" / "cache").mkdir(parents=True)
    (aws / "sso" / "cache" / "tok.json").write_text('{"accessToken":"SSO_SECRET"}')
    (aws / "cli" / "cache").mkdir(parents=True)
    (aws / "cli" / "cache" / "role.json").write_text('{"Credentials":"CACHED_SECRET"}')
    (aws / "credentials").write_text(credentials)
    (aws / "config").write_text(config)
    return aws


def _stage_bedrock(home, profile, dest):
    """Drive sandbox_stage_bedrock_credentials against a fake HOME; return rc."""
    script = (
        "source %s; HOME=%s; sandbox_stage_bedrock_credentials %s %s"
        % (SANDBOX_LIB, shlex.quote(str(home)), shlex.quote(profile),
           shlex.quote(str(dest)))
    )
    return subprocess.run(["bash", "-c", script], text=True, capture_output=True)


def test_stage_bedrock_credentials_copies_only_that_profile(tmp_path):
    # The whole reason for generating a credentials file instead of binding
    # ~/.aws: the SSO access token is exchangeable for role credentials across
    # every account the user can reach, so only the named profile's static keys
    # may cross into the sandbox.
    home = tmp_path / "home"
    home.mkdir()
    _fake_aws_home(home)
    dest = tmp_path / "chome" / ".aws"

    p = _stage_bedrock(home, "bedrock", dest)
    assert p.returncode == 0, p.stderr

    creds = (dest / "credentials").read_text()
    assert "[bedrock]" in creds
    assert "aws_access_key_id = AKIA_BEDROCK" in creds
    # Values containing '=' must survive verbatim (session tokens are base64).
    assert "aws_session_token = tok/en+with=padding==" in creds
    # No other identity, and nothing exchangeable for one.
    assert "AKIA_OTHER_IDENTITY" not in creds
    assert "[default]" not in creds
    assert "SSO_SECRET" not in creds and "CACHED_SECRET" not in creds
    assert sorted(os.listdir(dest)) == ["config", "credentials"]
    # Region is not a credential but the SDKs need it on the profile they read.
    assert "region = ap-southeast-2" in (dest / "config").read_text()
    # Credentials on disk stay private even inside the ephemeral home.
    assert oct(os.stat(dest / "credentials").st_mode)[-3:] == "600"
    assert oct(os.stat(dest).st_mode)[-3:] == "700"


def test_stage_bedrock_credentials_rejects_sso_only_profile(tmp_path):
    # An SSO-only profile cannot be resolved in the sandbox (that needs the SSO
    # token and a writable cache), so staging must fail rather than write a
    # credentials file that looks usable and then hangs.
    home = tmp_path / "home"
    home.mkdir()
    _fake_aws_home(home, credentials="[bedrock]\nsso_session = my-sso-bedrock\n")
    dest = tmp_path / "chome" / ".aws"

    p = _stage_bedrock(home, "bedrock", dest)
    assert p.returncode == 1, p.stdout + p.stderr
    assert not (dest / "credentials").exists()


def test_stage_bedrock_credentials_rewrite_is_atomic(tmp_path):
    # The broker rewrites this file underneath a live session, so a reader must
    # never see a truncated one: the write goes to a temp file and is renamed.
    home = tmp_path / "home"
    home.mkdir()
    _fake_aws_home(home)
    dest = tmp_path / "chome" / ".aws"
    assert _stage_bedrock(home, "bedrock", dest).returncode == 0
    first = (dest / "credentials").read_text()

    rotated = BEDROCK_CREDENTIALS.replace("AKIA_BEDROCK", "AKIA_ROTATED")
    (home / ".aws" / "credentials").write_text(rotated)
    assert _stage_bedrock(home, "bedrock", dest).returncode == 0

    assert "AKIA_ROTATED" in (dest / "credentials").read_text()
    assert "AKIA_BEDROCK" not in (dest / "credentials").read_text()
    assert first != (dest / "credentials").read_text()
    # No temp file left behind for the container to find.
    assert sorted(os.listdir(dest)) == ["config", "credentials"]


def _mqyolo_dry_run(tmp_path, home, args=("--no-broker",), extra_env=None):
    """Run mqyolo against a fake apptainer that records its argv and copies the
    ephemeral container home (which is otherwise deleted on exit) so the staged
    credentials can be inspected. Returns (proc, argv_lines, saved_home)."""
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir(parents=True, exist_ok=True)
    saved = tmp_path / "saved_chome"
    fake_apptainer = fakebin / "apptainer"
    # Find the /container_home bind and snapshot its source before mqyolo's EXIT
    # trap removes it. (No %-formatting here: the script is full of ${a%%...}.)
    # Fallback via the /etc/passwd workaround's bind, whose source is a file
    # inside the same ephemeral home: when the suite itself runs inside an mqyolo
    # sandbox, /container_home is already a mount, so mqyolo re-binds it read-only
    # from /proc/mounts and the dedupe drops the ephemeral home's own rw bind.
    script = """\
#!/bin/sh
rw= ; pw=
for a in "$@"; do
  case "$a" in
    *:/container_home:rw) rw="${a%%:*}" ;;
    *:/container_home/sandbox_passwd:ro) pw="$(dirname "${a%%:*}")" ;;
  esac
done
chome="${rw:-$pw}"
[ -n "$chome" ] && cp -a "$chome" @SAVED@
printf '%s\\n' "$@"
"""
    fake_apptainer.write_text(script.replace("@SAVED@", shlex.quote(str(saved))))
    fake_apptainer.chmod(0o755)
    fake_sif = tmp_path / "ai_tool.sif"
    fake_sif.write_text("")
    env = {
        **os.environ,
        "PATH": f"{fakebin}:{os.environ['PATH']}",
        "HOME": str(home),
        "AI_TOOL_SIF": str(fake_sif),
        # Pin the Bedrock inputs: the ambient environment of whoever runs the
        # suite must not decide whether staging happens.
        "CLAUDE_CODE_USE_BEDROCK": "",
        "AWS_PROFILE": "",
        "AWS_BEARER_TOKEN_BEDROCK": "",
        **(extra_env or {}),
    }
    p = subprocess.run([str(MQYOLO), *args], text=True, capture_output=True,
                       env=env, cwd=str(home))
    return p, (p.stdout + p.stderr).splitlines(), saved


def _bedrock_settings(home, profile="bedrock"):
    """Write the ~/.claude/settings.json env block that turns Bedrock on. This is
    where it really comes from: a plain login shell has none of it set, so mqyolo
    cannot rely on its own environment to detect Bedrock."""
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    (home / ".claude" / "settings.json").write_text(
        '{"env": {"CLAUDE_CODE_USE_BEDROCK": "1", "AWS_PROFILE": "%s"}}' % profile
    )


def test_mqyolo_stages_bedrock_profile_named_in_claude_settings(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    _fake_aws_home(home)
    _bedrock_settings(home)

    p, argv, saved = _mqyolo_dry_run(tmp_path, home)
    assert p.returncode == 0, p.stderr

    creds = (saved / ".aws" / "credentials").read_text()
    assert "AKIA_BEDROCK" in creds
    assert "AKIA_OTHER_IDENTITY" not in creds
    assert not (saved / ".aws" / "sso").exists()
    # Naming the profile is safe once only that profile is staged, and opencode
    # (which never reads Claude's settings.json) needs it.
    assert "AWS_PROFILE=bedrock" in argv
    # The real ~/.aws stays shadowed by an empty dir, not bound through.
    aws_real = os.path.realpath(home / ".aws")
    assert any(a.endswith(f":{aws_real}:ro") and "_empty_aws" in a for a in argv), argv


def test_mqyolo_warns_when_bedrock_profile_has_no_static_keys(tmp_path):
    # Silence is what made the original failure so hard to diagnose, so an
    # unusable profile must say so at launch rather than let the container hang.
    home = tmp_path / "home"
    home.mkdir()
    _fake_aws_home(home, credentials="[bedrock]\nsso_session = my-sso-bedrock\n")
    _bedrock_settings(home)

    p, argv, saved = _mqyolo_dry_run(tmp_path, home)
    assert p.returncode == 0, p.stderr
    assert "no static keys" in p.stderr, p.stderr
    assert "mqbedrock" in p.stderr
    assert not any(a.startswith("AWS_PROFILE=") for a in argv), argv


def test_mqyolo_stages_nothing_without_bedrock(tmp_path):
    # No Bedrock configured: ~/.aws stays entirely out of the sandbox.
    home = tmp_path / "home"
    home.mkdir()
    _fake_aws_home(home)

    p, argv, saved = _mqyolo_dry_run(tmp_path, home)
    assert p.returncode == 0, p.stderr
    assert not (saved / ".aws").exists()
    assert not any(a.startswith("AWS_PROFILE=") for a in argv), argv


def test_mqyolo_prefers_bedrock_api_key_over_staging(tmp_path):
    # A Bedrock API key is already scoped to model invocation, so there is no
    # reason to put any AWS profile in the sandbox at all.
    home = tmp_path / "home"
    home.mkdir()
    _fake_aws_home(home)
    _bedrock_settings(home)

    p, argv, saved = _mqyolo_dry_run(
        tmp_path, home, extra_env={"AWS_BEARER_TOKEN_BEDROCK": "bedrock-scoped-key"}
    )
    assert p.returncode == 0, p.stderr
    assert not (saved / ".aws").exists()
    assert "AWS_BEARER_TOKEN_BEDROCK=bedrock-scoped-key" in argv
    assert not any(a.startswith("AWS_PROFILE=") for a in argv), argv


def test_mqyolo_skips_staging_when_aws_is_opted_in(tmp_path):
    # --ro-paths ~/.aws is a deliberate choice to expose the real directory;
    # staging over it would silently narrow what the caller asked for.
    home = tmp_path / "home"
    home.mkdir()
    _fake_aws_home(home)
    _bedrock_settings(home)

    p, argv, saved = _mqyolo_dry_run(
        tmp_path, home, args=("--ro-paths", str(home / ".aws"), "--no-broker")
    )
    assert p.returncode == 0, p.stderr
    # sandbox_home_dotfiles left a symlink to the real dir; nothing was generated.
    assert (saved / ".aws").is_symlink()
    assert not (saved / ".aws" / "credentials").is_file() or \
        "AKIA_OTHER_IDENTITY" in (home / ".aws" / "credentials").read_text()


@pytest.mark.skipif(shutil.which("qsub") is None, reason="no batch queue: broker is skipped")
def test_mqyolo_proxies_mqbedrock_only_when_a_credential_was_staged(tmp_path):
    # The staged keys expire and cannot be refreshed from inside the sandbox, so
    # the session gets an mqbedrock stub to reach the host with — but only when
    # there is something to restage.
    home = tmp_path / "home"
    home.mkdir()
    _fake_aws_home(home)
    _bedrock_settings(home)

    p, argv, saved = _mqyolo_dry_run(tmp_path, home, args=())
    assert p.returncode == 0, p.stderr
    shims = saved / ".mqyolo" / "shims"
    assert (shims / "mqsub").is_symlink()
    assert (shims / "mqbedrock").is_symlink()

    # Without Bedrock there is no credential to refresh, so no stub.
    (home / ".claude" / "settings.json").write_text("{}")
    p, argv, saved2 = _mqyolo_dry_run(tmp_path / "second", home, args=())
    assert p.returncode == 0, p.stderr
    assert (saved2 / ".mqyolo" / "shims" / "mqsub").is_symlink()
    assert not (saved2 / ".mqyolo" / "shims" / "mqbedrock").exists()


def test_sandbox_build_env_disables_instance_metadata(tmp_path):
    # 169.254.169.254 is blackholed on this network, so an AWS SDK with no
    # credential hangs there instead of erroring. Fail fast instead.
    script = (
        "set -euo pipefail; source %s; "
        "unset AWS_EC2_METADATA_DISABLED AWS_METADATA_SERVICE_TIMEOUT "
        "AWS_METADATA_SERVICE_NUM_ATTEMPTS; "
        "ENV_ARGS=(); sandbox_build_env; "
        'for a in "${ENV_ARGS[@]}"; do [[ "$a" == --env ]] || echo "$a"; done'
        % SANDBOX_LIB
    )
    p = subprocess.run(["bash", "-c", script], text=True, capture_output=True)
    assert p.returncode == 0, p.stderr
    out = p.stdout.splitlines()
    assert "AWS_EC2_METADATA_DISABLED=true" in out
    assert "AWS_METADATA_SERVICE_TIMEOUT=1" in out
    assert "AWS_METADATA_SERVICE_NUM_ATTEMPTS=1" in out


def test_sandbox_build_env_honours_explicit_metadata_setting():
    # Apptainer forwards the environment, so an explicit host value must win
    # rather than be overridden by our default.
    script = (
        "set -euo pipefail; source %s; "
        "export AWS_EC2_METADATA_DISABLED=false; "
        "ENV_ARGS=(); sandbox_build_env; "
        'for a in "${ENV_ARGS[@]}"; do [[ "$a" == --env ]] || echo "$a"; done'
        % SANDBOX_LIB
    )
    p = subprocess.run(["bash", "-c", script], text=True, capture_output=True)
    assert p.returncode == 0, p.stderr
    assert "AWS_EC2_METADATA_DISABLED=true" not in p.stdout.splitlines()


def test_mqbedrock_forwards_itself_to_the_broker_inside_the_sandbox(tmp_path):
    # One awsAuthRefresh entry has to work on both sides of the container, so
    # mqbedrock detects the sandbox (MQBROKER_SPOOL) and re-execs the broker stub
    # rather than trying to refresh credentials it cannot reach.
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    stub = fakebin / "mqbedrock"
    stub.write_text("#!/bin/sh\necho STUB \"$@\"\n")
    stub.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{fakebin}:{os.environ['PATH']}",
        "MQBROKER_SPOOL": str(tmp_path / "spool"),
    }
    p = subprocess.run([str(MQBEDROCK)], text=True, capture_output=True, env=env)
    assert p.returncode == 0, p.stdout + p.stderr
    # --no-login is forced: the broker cannot display a device code.
    assert p.stdout.strip() == "STUB --no-login", p.stdout


def test_mqbedrock_reports_when_it_cannot_reach_the_host(tmp_path):
    # In the sandbox with no stub on PATH there is no way to refresh; say so
    # instead of failing somewhere deep in the AWS CLI.
    env = {
        **os.environ,
        # A usable PATH (the script needs bash) that holds no mqbedrock.
        "PATH": "/usr/bin:/bin",
        "MQBROKER_SPOOL": str(tmp_path / "spool"),
    }
    p = subprocess.run([str(MQBEDROCK)], text=True, capture_output=True, env=env)
    assert p.returncode == 1
    assert "cannot be refreshed" in p.stderr
    assert "Run 'mqbedrock' on the host" in p.stderr


def test_broker_rejects_mqbedrock_without_a_staged_credential():
    # Nothing to refresh means the container gains nothing by asking the host to
    # mint credentials, so the capability is not offered at all.
    with running_broker() as (spool, shim, *_):
        mqbedrock = _stub_as(shim, "mqbedrock")
        rc, out = _run_stub(mqbedrock, spool)
        assert rc == 126, out
        assert "not permitted" in out


def _broker_with_fake_mqbedrock(tmp_path, script):
    """A broker whose SCRIPT_DIR contains a fake mqbedrock (the broker runs
    ${SCRIPT_DIR}/<cmd>), so the real SSO flow is never touched."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    # Copies, not symlinks: the broker resolves its SCRIPT_DIR through realpath,
    # so a symlink would send it back to the real bin/ and run the real mqbedrock.
    for name in ("mqsub-broker", "_sandbox_common.bash"):
        shutil.copy2(BIN / name, bindir / name)
    fake = bindir / "mqbedrock"
    fake.write_text(script)
    fake.chmod(0o755)
    return bindir / "mqsub-broker"


def test_broker_restages_bedrock_credentials_after_a_refresh(tmp_path):
    # The point of routing mqbedrock through the broker: after the host mints new
    # keys, the LIVE session must pick them up (Claude re-resolves static keys
    # periodically) instead of needing a relaunch.
    home = tmp_path / "home"
    home.mkdir()
    _fake_aws_home(home)
    dest = tmp_path / "chome" / ".aws"
    assert _stage_bedrock(home, "bedrock", dest).returncode == 0
    assert "AKIA_BEDROCK" in (dest / "credentials").read_text()

    # Stand in for the real refresh: rotate the host's static keys.
    rotated = BEDROCK_CREDENTIALS.replace("AKIA_BEDROCK", "AKIA_ROTATED")
    broker_path = _broker_with_fake_mqbedrock(
        tmp_path,
        "#!/bin/sh\ncat > %s <<'EOF'\n%s\nEOF\necho refreshed\n"
        % (shlex.quote(str(home / ".aws" / "credentials")), rotated.rstrip("\n")),
    )

    env_home = os.environ.get("HOME")
    try:
        # sandbox_stage_bedrock_credentials reads $HOME/.aws in the broker.
        os.environ["HOME"] = str(home)
        with running_broker(bedrock=("bedrock", dest),
                            broker_path=broker_path) as (spool, shim, *_):
            mqbedrock = _stub_as(shim, "mqbedrock")
            rc, out = _run_stub(mqbedrock, spool)
            assert rc == 0, out
            assert "refreshed" in out
    finally:
        if env_home is not None:
            os.environ["HOME"] = env_home

    assert "AKIA_ROTATED" in (dest / "credentials").read_text()


def test_broker_refreshes_the_profile_staged_for_the_session(tmp_path):
    # A caller-selected Codex profile can name a different static AWS profile.
    # The broker must refresh that same profile on the host before restaging it,
    # while still rejecting any profile choice sent from inside the container.
    home = tmp_path / "home"
    home.mkdir()
    alt_credentials = BEDROCK_CREDENTIALS + """\

[bedrock-alt]
aws_access_key_id = AKIA_ALT
aws_secret_access_key = alt-secret
aws_session_token = alt-token
"""
    alt_config = BEDROCK_CONFIG + """\

[profile bedrock-alt]
region = ap-southeast-2
"""
    _fake_aws_home(home, credentials=alt_credentials, config=alt_config)
    dest = tmp_path / "chome" / ".aws"
    assert _stage_bedrock(home, "bedrock-alt", dest).returncode == 0

    arg_log = tmp_path / "mqbedrock.args"
    rotated = alt_credentials.replace("AKIA_ALT", "AKIA_ALT_ROTATED")
    broker_path = _broker_with_fake_mqbedrock(
        tmp_path,
        "#!/bin/sh\nprintf '%s\\n' \"$@\" > %s\ncat > %s <<'EOF'\n%s\nEOF\necho refreshed\n"
        % (
            shlex.quote(str(arg_log)),
            shlex.quote(str(home / ".aws" / "credentials")),
            rotated.rstrip("\n"),
        ),
    )

    env_home = os.environ.get("HOME")
    try:
        os.environ["HOME"] = str(home)
        with running_broker(
            bedrock=("bedrock-alt", dest), broker_path=broker_path
        ) as (spool, shim, *_):
            mqbedrock = _stub_as(shim, "mqbedrock")
            rc, out = _run_stub(mqbedrock, spool)
            assert rc == 0, out
    finally:
        if env_home is not None:
            os.environ["HOME"] = env_home

    assert arg_log.read_text().splitlines() == [
        "--no-login",
        "--static-profile",
        "bedrock-alt",
    ]
    staged = (dest / "credentials").read_text()
    assert "AKIA_ALT_ROTATED" in staged
    assert "AKIA_BEDROCK" not in staged


def test_broker_rejects_extra_mqbedrock_arguments(tmp_path):
    # mqbedrock takes no arguments from the container: --no-login is forced, and
    # anything else could steer the host-side refresh.
    dest = tmp_path / "chome" / ".aws"
    dest.mkdir(parents=True)
    with running_broker(bedrock=("bedrock", dest)) as (spool, shim, *_):
        mqbedrock = _stub_as(shim, "mqbedrock")
        rc, out = _run_stub(mqbedrock, spool, "--profile", "somethingelse")
        assert rc == 126, out
        assert "accepts no arguments" in out


# ---------------------------------------------------------------------------
# Codex on Bedrock: `mqbedrock --setup-codex` writes a Codex profile file on the
# host naming the static AWS profile, and mqyolo selects it and stages that
# profile's keys. Codex speaks only the OpenAI protocol, so this runs the OpenAI
# models on Bedrock's OpenAI-compatible endpoint, not Claude.
# ---------------------------------------------------------------------------
def _setup_codex(home, *extra):
    """Run `mqbedrock --setup-codex` against a fake HOME."""
    env = {**os.environ, "HOME": str(home)}
    # Whoever runs the suite may be inside an mqyolo sandbox; the setup must be
    # tested as it behaves on the host (it does not forward to the broker).
    env.pop("MQBROKER_SPOOL", None)
    env.pop("CODEX_HOME", None)
    return subprocess.run([str(MQBEDROCK), "--setup-codex", *extra],
                          text=True, capture_output=True, env=env)


def _setup_claude(home, *extra):
    """Run `mqbedrock --setup-claude` against a fake HOME."""
    env = {**os.environ, "HOME": str(home)}
    env.pop("MQBROKER_SPOOL", None)
    return subprocess.run([str(MQBEDROCK), "--setup-claude", *extra],
                          text=True, capture_output=True, env=env)


def test_mqbedrock_setup_claude_writes_both_settings_files(tmp_path):
    home = tmp_path / "home"
    home.mkdir()

    p = _setup_claude(home)
    assert p.returncode == 0, p.stdout + p.stderr

    settings = json.loads((home / ".claude" / "settings.json").read_text())
    # The refresh hook is this same script, which works on the host and (via the
    # broker stub) inside a sandbox.
    assert settings["awsAuthRefresh"] == "mqbedrock"
    env = settings["env"]
    assert env["CLAUDE_CODE_USE_BEDROCK"] == "1"
    assert env["AWS_REGION"] == "ap-southeast-2"
    # The profile mqbedrock keeps topped up, and the au.* inference profiles that
    # keep inference in Australia.
    assert env["AWS_PROFILE"] == "bedrock"
    assert env["ANTHROPIC_DEFAULT_SONNET_MODEL"] == "au.anthropic.claude-sonnet-5"
    assert env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "au.anthropic.claude-opus-5[1m]"
    assert env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == \
        "au.anthropic.claude-haiku-4-5-20251001-v1:0"
    # Onboarding is pre-answered: its interactive walkthrough has nothing to log
    # into when the credential is an AWS profile.
    assert json.loads((home / ".claude.json").read_text()) == \
        {"hasCompletedOnboarding": True}
    # Writing config needs no credentials, so it works before the first refresh.
    assert not (home / ".aws").exists()


def test_mqbedrock_setup_claude_accepts_an_equivalent_file(tmp_path):
    # Same JSON, different key order and whitespace: nothing to do, no complaint.
    home = tmp_path / "home"
    home.mkdir()
    assert _setup_claude(home).returncode == 0
    settings = home / ".claude" / "settings.json"
    reordered = json.loads(settings.read_text())
    reordered["env"] = dict(reversed(list(reordered["env"].items())))
    settings.write_text(json.dumps(reordered, separators=(",", ":")))

    p = _setup_claude(home)
    assert p.returncode == 0, p.stdout + p.stderr
    assert "already holds these settings" in p.stdout
    # Left byte-for-byte alone, not rewritten.
    assert settings.read_text() == json.dumps(reordered, separators=(",", ":"))


def test_mqbedrock_setup_claude_refuses_to_clobber_different_settings(tmp_path):
    # ~/.claude/settings.json and ~/.claude.json are the user's own files and
    # ~/.claude.json accumulates real state, so a mismatch must stop.
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude").mkdir()
    (home / ".claude" / "settings.json").write_text('{"model": "opus"}')
    (home / ".claude.json").write_text('{"projects": {"/tmp": {}}}')

    p = _setup_claude(home)
    assert p.returncode == 1
    assert "differs from what --setup-claude" in p.stderr
    assert "--force" in p.stderr
    assert json.loads((home / ".claude" / "settings.json").read_text()) == \
        {"model": "opus"}
    assert json.loads((home / ".claude.json").read_text()) == \
        {"projects": {"/tmp": {}}}


def test_mqbedrock_setup_claude_force_overwrites_and_keeps_a_backup(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude").mkdir()
    (home / ".claude" / "settings.json").write_text('{"model": "opus"}')
    (home / ".claude.json").write_text('{"projects": {"/tmp": {}}}')

    p = _setup_claude(home, "--force")
    assert p.returncode == 0, p.stdout + p.stderr
    assert json.loads((home / ".claude" / "settings.json").read_text())["env"][
        "CLAUDE_CODE_USE_BEDROCK"] == "1"
    assert json.loads((home / ".claude.json").read_text()) == \
        {"hasCompletedOnboarding": True}
    # The replaced state is recoverable.
    backups = sorted(f.name for f in (home / ".claude").iterdir()
                     if f.name.startswith("settings.json.mqbedrock-"))
    assert len(backups) == 1, list((home / ".claude").iterdir())
    assert json.loads((home / ".claude" / backups[0]).read_text()) == {"model": "opus"}
    assert any(f.name.startswith(".claude.json.mqbedrock-") for f in home.iterdir())


def test_mqbedrock_force_without_a_setup_option_is_rejected(tmp_path):
    # --force on a plain refresh would silently mean nothing.
    env = {**os.environ, "HOME": str(tmp_path)}
    env.pop("MQBROKER_SPOOL", None)
    p = subprocess.run([str(MQBEDROCK), "--force"], text=True, capture_output=True,
                       env=env)
    assert p.returncode == 2
    assert "--force only applies" in p.stderr


def test_mqbedrock_setup_codex_writes_a_codex_profile_file(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    _fake_aws_home(home)

    p = _setup_codex(home)
    assert p.returncode == 0, p.stdout + p.stderr

    toml = (home / ".codex" / "bedrock.config.toml").read_text()
    # The native OpenAI-compatible Bedrock Runtime provider used by current Codex.
    assert 'model_provider = "amazon-bedrock-runtime"' in toml
    assert 'model = "global.openai.gpt-5.6-sol"' in toml
    assert 'model_reasoning_effort = "high"' in toml
    # Signed with the same static profile mqbedrock keeps topped up for Claude,
    # in that profile's own region.
    assert "[model_providers.amazon-bedrock-runtime.aws]" in toml
    assert 'profile = "bedrock"' in toml
    assert 'region = "ap-southeast-2"' in toml
    # Current Codex only accepts `aws` as aws.auth_refresh.command; mqbedrock is
    # deliberately absent so the generated profile passes provider validation.
    assert "aws.auth_refresh" not in toml
    # It is a profile layer, so it does nothing until `codex --profile bedrock`:
    # the user's own config.toml must not be touched.
    assert not (home / ".codex" / "config.toml").exists()
    # Writing config needs no credentials, so it works before the first refresh.
    assert not (home / ".aws" / "credentials.new").exists()
    # Setup makes the external IAM prerequisite explicit. Profile generation can
    # succeed without the QUT role being authorized to invoke project/default.
    assert "IAM prerequisite" in p.stdout
    assert "arn:aws:bedrock:ap-southeast-2:267451755618:project/default" in p.stdout
    assert "Ready-to-attach policy for this model and Region" in p.stdout

    # Idempotent: a second run leaves the file alone.
    again = _setup_codex(home)
    assert again.returncode == 0, again.stdout + again.stderr
    assert "already up to date" in again.stdout
    assert (home / ".codex" / "bedrock.config.toml").read_text() == toml


def test_mqbedrock_setup_codex_needs_no_aws_config_or_credentials(tmp_path):
    # A fresh machine: no ~/.aws at all. The Codex profile is still written (with
    # the default region) and no AWS call is made, so the two setup steps —
    # config now, credentials later — are independent.
    home = tmp_path / "home"
    home.mkdir()

    p = _setup_codex(home, "--codex-model", "global.openai.gpt-6-astra")
    assert p.returncode == 0, p.stdout + p.stderr

    toml = (home / ".codex" / "bedrock.config.toml").read_text()
    assert 'model = "global.openai.gpt-6-astra"' in toml
    assert 'region = "ap-southeast-2"' in toml
    assert not (home / ".aws").exists()


def test_mqbedrock_setup_codex_can_name_an_alternate_static_profile(tmp_path):
    home = tmp_path / "home"
    home.mkdir()

    p = _setup_codex(home, "--static-profile", "bedrock-alt")
    assert p.returncode == 0, p.stdout + p.stderr
    toml = (home / ".codex" / "bedrock.config.toml").read_text()
    assert 'profile = "bedrock-alt"' in toml
    assert "signing with the [bedrock-alt] profile" in p.stdout


def test_mqbedrock_setup_requires_editing_policy_for_model_or_region_overrides(tmp_path):
    home = tmp_path / "home"
    (home / ".aws").mkdir(parents=True)
    (home / ".aws" / "config").write_text(
        "[profile bedrock]\nregion = us-east-1\n"
    )

    p = _setup_codex(home, "--codex-model", "global.openai.gpt-5.6")
    assert p.returncode == 0, p.stdout + p.stderr
    assert "Policy example (written for global.openai.gpt-5.6-sol in ap-southeast-2)" in p.stdout
    assert "Edit every model and Region ARN/condition" in p.stdout
    assert "to match this\nprofile before" in p.stdout


@pytest.mark.parametrize("setup_option", ["--setup-codex", "--setup-claude"])
def test_mqbedrock_setup_needs_no_aws_cli_or_cluster_pixi(tmp_path, setup_option):
    # Setup dispatch must happen before locating the AWS CLI. Simulate a plain
    # machine with neither aws nor the central pixi manifest.
    home = tmp_path / "home"
    home.mkdir()
    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": "/usr/bin:/bin",
        "PIXI_CENTRAL_TOML": str(tmp_path / "missing-pixi.toml"),
    }
    env.pop("MQBROKER_SPOOL", None)
    env.pop("CODEX_HOME", None)

    p = subprocess.run(
        [str(MQBEDROCK), setup_option], text=True, capture_output=True, env=env
    )
    assert p.returncode == 0, p.stdout + p.stderr
    if setup_option == "--setup-codex":
        assert (home / ".codex" / "bedrock.config.toml").exists()
    else:
        assert (home / ".claude" / "settings.json").exists()


def test_mqbedrock_setup_warns_about_an_old_host_codex(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    fake_codex = fakebin / "codex"
    fake_codex.write_text("#!/bin/sh\necho 'codex-cli 0.144.0-alpha.4'\n")
    fake_codex.chmod(0o755)
    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{fakebin}:{os.environ['PATH']}",
    }
    env.pop("MQBROKER_SPOOL", None)
    env.pop("CODEX_HOME", None)

    p = subprocess.run(
        [str(MQBEDROCK), "--setup-codex"], text=True, capture_output=True, env=env
    )
    assert p.returncode == 0, p.stdout + p.stderr
    assert "host Codex 0.144.0 is too old" in p.stderr
    assert "at least 0.149.1" in p.stderr


@pytest.mark.skipif(shutil.which("codex") is None, reason="Codex CLI is not installed")
def test_mqbedrock_setup_codex_profile_loads_in_current_codex(tmp_path):
    # Parse the complete generated profile through Codex without making a model
    # request. This catches provider-schema failures that string assertions miss.
    home = tmp_path / "home"
    home.mkdir()
    assert _setup_codex(home).returncode == 0
    env = {**os.environ, "HOME": str(home), "CODEX_HOME": str(home / ".codex")}
    p = subprocess.run(
        ["codex", "-p", "bedrock", "debug", "prompt-input", "test"],
        text=True,
        capture_output=True,
        env=env,
        cwd=str(home),
    )
    assert p.returncode == 0, p.stdout + p.stderr


def test_mqbedrock_setup_codex_refuses_a_file_it_did_not_write(tmp_path):
    # Codex profile files are hand-written by users too; only ones carrying our
    # marker may be replaced.
    home = tmp_path / "home"
    home.mkdir()
    (home / ".codex").mkdir()
    mine = home / ".codex" / "bedrock.config.toml"
    mine.write_text('model = "gpt-5.5"\n')

    p = _setup_codex(home)
    assert p.returncode == 1
    assert "was not written by --setup-codex" in p.stderr
    assert "--codex-profile" in p.stderr
    assert mine.read_text() == 'model = "gpt-5.5"\n'


def test_mqyolo_codex_selects_the_bedrock_profile_and_stages_its_aws_profile(tmp_path):
    # No Claude settings.json here: for codex the Bedrock decision is recorded in
    # the Codex profile file, and mqyolo has to read the AWS profile out of it.
    home = tmp_path / "home"
    home.mkdir()
    _fake_aws_home(home)
    assert _setup_codex(home).returncode == 0

    p, argv, saved = _mqyolo_dry_run(tmp_path, home, args=("--no-broker", "codex"))
    assert p.returncode == 0, p.stderr

    assert "--profile" in argv, argv
    assert argv[argv.index("--profile") + 1] == "bedrock", argv
    # Codex prefers a configured aws.profile over every other credential, so that
    # profile — and only it — has to be in the sandbox.
    assert "AWS_PROFILE=bedrock" in argv
    creds = (saved / ".aws" / "credentials").read_text()
    assert "AKIA_BEDROCK" in creds
    assert "AKIA_OTHER_IDENTITY" not in creds


def test_mqyolo_codex_stages_the_aws_profile_named_in_the_codex_file(tmp_path):
    # The name is read from the file, not assumed: a profile file written with a
    # different AWS profile must stage that one.
    home = tmp_path / "home"
    home.mkdir()
    _fake_aws_home(
        home,
        credentials=BEDROCK_CREDENTIALS + "\n[bedrock-alt]\n"
        "aws_access_key_id = AKIA_ALT\naws_secret_access_key = alt-secret\n",
    )
    assert _setup_codex(home).returncode == 0
    profile_file = home / ".codex" / "bedrock.config.toml"
    profile_file.write_text(
        profile_file.read_text().replace('profile = "bedrock"', 'profile = "bedrock-alt"')
    )

    p, argv, saved = _mqyolo_dry_run(tmp_path, home, args=("--no-broker", "codex"))
    assert p.returncode == 0, p.stderr
    assert "AWS_PROFILE=bedrock-alt" in argv, argv
    creds = (saved / ".aws" / "credentials").read_text()
    assert "AKIA_ALT" in creds
    assert "AKIA_BEDROCK" not in creds


def test_mqyolo_codex_no_bedrock_leaves_the_profile_unselected(tmp_path):
    # --no-bedrock for codex: the file stays on disk but is never activated, and
    # no AWS credentials are staged.
    home = tmp_path / "home"
    home.mkdir()
    _fake_aws_home(home)
    assert _setup_codex(home).returncode == 0

    p, argv, saved = _mqyolo_dry_run(
        tmp_path, home, args=("--no-broker", "--no-bedrock", "codex")
    )
    assert p.returncode == 0, p.stderr
    assert "--profile" not in argv, argv
    assert not any(a.startswith("AWS_PROFILE=") for a in argv), argv
    assert not (saved / ".aws").exists()


def test_mqyolo_codex_does_not_override_a_caller_supplied_profile(tmp_path):
    # Two --profile flags would be an error, and selecting a non-Bedrock profile
    # must not expose credentials from the default Bedrock profile either.
    home = tmp_path / "home"
    home.mkdir()
    _fake_aws_home(home)
    assert _setup_codex(home).returncode == 0

    p, argv, saved = _mqyolo_dry_run(
        tmp_path, home, args=("--no-broker", "codex", "--profile", "mine")
    )
    assert p.returncode == 0, p.stderr
    assert argv.count("--profile") == 1, argv
    assert argv[argv.index("--profile") + 1] == "mine", argv
    assert not any(a.startswith("AWS_PROFILE=") for a in argv), argv
    assert not (saved / ".aws").exists()


def test_mqyolo_codex_recognizes_an_attached_short_profile(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    _fake_aws_home(home)
    assert _setup_codex(home).returncode == 0

    p, argv, saved = _mqyolo_dry_run(
        tmp_path, home, args=("--no-broker", "codex", "-pmine")
    )
    assert p.returncode == 0, p.stderr
    assert "-pmine" in argv, argv
    assert "--profile" not in argv, argv
    assert not any(a.startswith("AWS_PROFILE=") for a in argv), argv
    assert not (saved / ".aws").exists()


def test_mqyolo_codex_stages_credentials_for_the_caller_selected_profile(tmp_path):
    # When the caller's profile is itself a Bedrock profile, stage the AWS
    # identity named by that file rather than the default Bedrock identity.
    home = tmp_path / "home"
    home.mkdir()
    _fake_aws_home(
        home,
        credentials=BEDROCK_CREDENTIALS + "\n[bedrock-alt]\n"
        "aws_access_key_id = AKIA_ALT\naws_secret_access_key = alt-secret\n",
    )
    assert _setup_codex(home).returncode == 0
    (home / ".codex" / "mine.config.toml").write_text(
        'model_provider = "amazon-bedrock-runtime"\n'
        '[model_providers.amazon-bedrock-runtime.aws]\n'
        'profile = "bedrock-alt"\nregion = "ap-southeast-2"\n'
    )

    p, argv, saved = _mqyolo_dry_run(
        tmp_path, home, args=("--no-broker", "codex", "--profile=mine")
    )
    assert p.returncode == 0, p.stderr
    assert "--profile=mine" in argv, argv
    assert "--profile" not in argv, argv
    assert "AWS_PROFILE=bedrock-alt" in argv, argv
    creds = (saved / ".aws" / "credentials").read_text()
    assert "AKIA_ALT" in creds
    assert "AKIA_BEDROCK" not in creds


def test_mqyolo_codex_points_at_the_setup_command_when_unauthenticated(tmp_path):
    # Bedrock is configured for Claude but codex has not been set up for it and
    # has no other credential: say so at launch instead of opening on a login
    # prompt that cannot be completed in the sandbox.
    home = tmp_path / "home"
    home.mkdir()
    _fake_aws_home(home)
    _bedrock_settings(home)

    p, argv, saved = _mqyolo_dry_run(
        tmp_path, home, args=("--no-broker", "codex"),
        extra_env={"OPENAI_API_KEY": ""},
    )
    assert p.returncode == 0, p.stderr
    assert "mqbedrock --setup-codex" in p.stderr
    assert "--profile" not in argv, argv

    # Quiet once codex has its own credentials.
    (home / ".codex").mkdir(exist_ok=True)
    (home / ".codex" / "auth.json").write_text('{"OPENAI_API_KEY": "sk-test"}')
    p, argv, saved = _mqyolo_dry_run(
        tmp_path, home, args=("--no-broker", "codex"),
        extra_env={"OPENAI_API_KEY": ""},
    )
    assert p.returncode == 0, p.stderr
    assert "--setup-codex" not in p.stderr


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
