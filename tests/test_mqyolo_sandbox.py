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
    # opencode is auto-approved with --auto, and both of the directories it keeps
    # state in (config + global AGENTS.md, and auth/session storage) are bound
    # read-write with XDG pinned to the container home so a host XDG_* cannot
    # redirect it onto the read-only real home.
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
        # A host XDG_CONFIG_HOME must not leak through to opencode.
        "XDG_CONFIG_HOME": str(fake_home / "xdg_config"),
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
    assert "XDG_CONFIG_HOME=/container_home/.config" in out
    assert "XDG_DATA_HOME=/container_home/.local/share" in out


def test_mqyolo_opencode_xdg_survives_user_bashrc(tmp_path):
    # An apptainer --env value is applied BEFORE the container sources the user's
    # real ~/.bashrc, so a bashrc that exports XDG_CONFIG_HOME would win and send
    # opencode's config/auth back onto the read-only real home. The shim bashrc
    # must re-assert the pins after sourcing the real one (same fix as PATH).
    real = tmp_path / "real_bashrc"
    real.write_text(
        'export XDG_CONFIG_HOME="$HOME/decoy_config"\n'
        'export XDG_DATA_HOME="$HOME/decoy_data"\n'
    )
    dest = tmp_path / "dest_bashrc"
    script = (
        "set -euo pipefail; source %s; "
        "sandbox_write_shim_bashrc %s %s /shims "
        "XDG_CONFIG_HOME=/container_home/.config "
        "XDG_DATA_HOME=/container_home/.local/share; "
        "HOME=/container_home; source %s; "
        'printf "%%s\\n%%s\\n" "$XDG_CONFIG_HOME" "$XDG_DATA_HOME"'
        % (SANDBOX_LIB, shlex.quote(str(dest)), shlex.quote(str(real)),
           shlex.quote(str(dest)))
    )
    p = subprocess.run(["bash", "-c", script], text=True, capture_output=True)
    assert p.returncode == 0, p.stderr
    assert p.stdout.split() == [
        "/container_home/.config",
        "/container_home/.local/share",
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
    chome = tmp_path / "chome"
    chome.mkdir()
    # As sandbox_home_dotfiles leaves them: symlinks onto the real home.
    (chome / ".config").symlink_to(home / ".config")
    (chome / ".local").symlink_to(home / ".local")
    config = home / ".config" / "opencode"
    data = home / ".local" / "share" / "opencode"
    guidance = tmp_path / "guidance.md"
    guidance.write_text("use mqsub\n")

    script = (
        "set -euo pipefail\n"
        "source %s; "
        "HOME=%s CONTAINER_HOME=%s; "
        "BIND_ARGS=(); "
        "sandbox_bind_opencode_home %s %s %s; "
        'for a in "${BIND_ARGS[@]}"; do [[ "$a" == --bind ]] || printf "%%s\\n" "$a"; done'
        % (
            SANDBOX_LIB,
            shlex.quote(str(home)),
            shlex.quote(str(chome)),
            shlex.quote(str(config)),
            shlex.quote(str(data)),
            shlex.quote(str(guidance)),
        )
    )
    p = subprocess.run(["bash", "-c", script], text=True, capture_output=True)
    assert p.returncode == 0, p.stderr
    # Both state dirs are created in the real home so the binds have a source.
    assert config.is_dir() and data.is_dir()
    binds = p.stdout.splitlines()
    assert f"{config}:/container_home/.config/opencode:rw" in binds
    assert f"{data}:/container_home/.local/share/opencode:rw" in binds
    assert f"{guidance}:/container_home/.config/opencode/AGENTS.md:ro" in binds
    # The XDG parents are now real dirs inside the ephemeral home, with the
    # opencode mountpoints present and NOT symlinked at the real home.
    for rel in (".config", ".local", ".local/share"):
        assert (chome / rel).is_dir() and not (chome / rel).is_symlink()
    assert (chome / ".config" / "opencode").is_dir()
    assert not (chome / ".config" / "opencode").is_symlink()
    assert (chome / ".local" / "share" / "opencode").is_dir()
    assert not (chome / ".local" / "share" / "opencode").is_symlink()


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
        "sandbox_bind_opencode_home %s %s %s; "
        'for a in "${BIND_ARGS[@]}"; do [[ "$a" == --bind ]] || printf "%%s\\n" "$a"; done'
        % (
            SANDBOX_LIB,
            shlex.quote(str(home)),
            shlex.quote(str(chome)),
            shlex.quote(str(home / ".config" / "opencode")),
            shlex.quote(str(home / ".local" / "share" / "opencode")),
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
