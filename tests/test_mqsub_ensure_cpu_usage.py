"""Integration tests for mqsub --ensure-cpu-usage.

Submits real PBS jobs via mqsub and waits for them to complete, so each test
takes several minutes plus queue time. Skipped automatically when qsub is not
available on PATH.
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
MQSUB = REPO / "bin" / "mqsub"

INTERVAL_SECONDS = 120
THRESHOLD_PERCENT = 50
JOB_TIMEOUT_SECONDS = 30 * 60

pytestmark = pytest.mark.skipif(
    shutil.which("qsub") is None,
    reason="qsub not available; --ensure-cpu-usage integration tests need a PBS queue",
)


@pytest.fixture
def shared_cwd():
    # PBS writes the job's .o/.e files to the submission cwd on the compute
    # node; if cwd is /tmp it won't be visible to the head node. Use $HOME.
    base = Path.home() / ".mqsub_pytest"
    base.mkdir(exist_ok=True)
    path = Path(tempfile.mkdtemp(dir=str(base)))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _run_mqsub(extra_args, cwd, stdin=None):
    cmd = [
        sys.executable, str(MQSUB),
        "-t", "1",
        "--hours", "1",
        "--no-email",
        "--no-executable-check",
        "--ensure-cpu-usage",
        "--ensure-cpu-usage-interval={}".format(INTERVAL_SECONDS),
        "--ensure-cpu-usage-threshold={}".format(THRESHOLD_PERCENT),
    ] + list(extra_args)
    return subprocess.run(
        cmd,
        input=stdin,
        capture_output=True,
        text=True,
        cwd=str(cwd),
        timeout=JOB_TIMEOUT_SECONDS,
    )


def test_ensure_cpu_usage_passes_when_cpu_busy(shared_cwd):
    # Busy-loop in bash for one interval plus a margin, so the monitor fires
    # exactly one check (~100% utilisation) and the job then exits 0.
    busy_seconds = INTERVAL_SECONDS + 60
    script = (
        "#!/bin/bash\n"
        "end=$((SECONDS + {}))\n".format(busy_seconds) +
        "while [ $SECONDS -lt $end ]; do :; done\n"
        "exit 0\n"
    )
    result = _run_mqsub(["--script", "-"], cwd=shared_cwd, stdin=script)
    assert result.returncode == 0, (
        "expected exit 0, got {}\nstdout:\n{}\nstderr:\n{}".format(
            result.returncode, result.stdout, result.stderr
        )
    )
    assert "--ensure-cpu-usage check failed" not in result.stderr


def test_ensure_cpu_usage_kills_idle_job(shared_cwd):
    # Sleep well past the first check window; the monitor should cancel the job
    # at the first interval boundary and the wrapper should exit 151.
    idle_seconds = INTERVAL_SECONDS * 3
    result = _run_mqsub(["--", "sleep", str(idle_seconds)], cwd=shared_cwd)
    assert result.returncode == 151, (
        "expected exit 151, got {}\nstdout:\n{}\nstderr:\n{}".format(
            result.returncode, result.stdout, result.stderr
        )
    )
    assert "--ensure-cpu-usage check failed" in result.stderr
