"""Tests for mqpixi.

test_mqpixi_aviary_warning is cheap: it stubs out pixi and a manifest, so it
runs anywhere.

test_mqpixi_aviary_env submits a real PBS job via mqsub and waits for it to
complete, so it takes several minutes plus queue time. It is skipped
automatically when qsub is not available on PATH.

# pixi run -e dev pytest tests/test_mqpixi.py
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pexpect
import pytest

REPO = Path(__file__).resolve().parents[1]
MQSUB = REPO / "bin" / "mqsub"
MQPIXI = REPO / "bin" / "mqpixi"

AVIARY_ENV = "aviary-v0-13-0"
_PIXI_TOML = Path("/pkg/cmr/mqpixi/pixi.toml")
AVIARY_PIXI_BIN = str(_PIXI_TOML.resolve().parent / ".pixi" / "envs" / AVIARY_ENV / "bin" / "aviary")

EXPECTED_ENV_VARS = {
    "GTDBTK_DATA_PATH": "/work/microbiome/db/gtdb/gtdb_release232/auxillary_files/gtdbtk_package/full_package/release232",
    "CHECKM2DB": "/work/microbiome/db/CheckM2_database/uniref100.KO.1.dmnd",
    "EGGNOG_DATA_DIR": "/mnt/hpccs01/work/microbiome/db/eggnog-mapper/2.1.3",
    "SINGLEM_METAPACKAGE_PATH": "/work/microbiome/db/singlem/S6.5.0.GTDB_r232.metapackage_20260319.smpkg.zb",
    "METABULI_DB_PATH": "/work/microbiome/db/metabuli/2024-3-28-GTDB214.1+humanT2T",
}

JOB_TIMEOUT_SECONDS = 30 * 60

AVIARY_WARNING_SNIPPET = "aviary .. --snakemake-profile aqua .."


@pytest.fixture
def stub_pixi(tmp_path):
    """A fake manifest plus a pixi stub that just echoes its arguments.

    Returns the env to run mqpixi with, so the aviary warning can be checked
    without a real pixi environment (or a queue).
    """
    manifest = tmp_path / "pixi.toml"
    manifest.write_text(
        "[environments]\n"
        "aviary-v0-13-0 = [\"a\"]\n"
        "aviary-v0-9-0 = [\"b\"]\n"
        "checkm2-v1-0-2 = [\"c\"]\n"
    )
    bindir = tmp_path / "bin"
    bindir.mkdir()
    pixi = bindir / "pixi"
    pixi.write_text('#!/bin/bash\necho "PIXI ARGS: $*"\n')
    pixi.chmod(0o755)

    env = dict(os.environ)
    env["PATH"] = "{}:{}".format(bindir, env["PATH"])
    env["CMR_PIXI_TOML"] = str(manifest)
    return env


@pytest.mark.parametrize(
    "requested,resolved,expect_warning",
    [
        # Bare program name resolves to the most recent aviary environment.
        ("aviary", "aviary-v0-13-0", True),
        # Exact (dotted) environment name.
        ("aviary-v0.9.0", "aviary-v0-9-0", True),
        # Non-aviary environments must not be warned about.
        ("checkm2-v1.0.2", "checkm2-v1-0-2", False),
    ],
)
def test_mqpixi_aviary_warning(stub_pixi, requested, resolved, expect_warning):
    """mqpixi warns to use --snakemake-profile aqua for any aviary environment."""
    proc = subprocess.run(
        [str(MQPIXI), requested],
        env=stub_pixi,
        capture_output=True,
        text=True,
        check=True,
    )

    assert "-e {}".format(resolved) in proc.stdout, (
        "expected pixi shell -e {}\nstdout:\n{}".format(resolved, proc.stdout)
    )
    if expect_warning:
        assert AVIARY_WARNING_SNIPPET in proc.stderr, (
            "expected aviary warning for {}\nstderr:\n{}".format(requested, proc.stderr)
        )
    else:
        assert AVIARY_WARNING_SNIPPET not in proc.stderr, (
            "unexpected aviary warning for {}\nstderr:\n{}".format(requested, proc.stderr)
        )


@pytest.fixture
def shared_cwd():
    # PBS writes the job's .o/.e files to the submission cwd on the compute
    # node; if cwd is /tmp it won't be visible to the head node. Use $HOME.
    base = Path.home() / ".mqpixi_pytest"
    base.mkdir(exist_ok=True)
    path = Path(tempfile.mkdtemp(dir=str(base)))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.mark.skipif(
    shutil.which("qsub") is None,
    reason="qsub not available; mqpixi integration tests need a PBS queue",
)
def test_mqpixi_aviary_env(shared_cwd):
    """Shell into aviary-v0-13-0 via mqpixi, submit a job that checks binary path and env vars."""
    job_cmd = " && ".join(
        ["which aviary"]
        + ['echo "{}=${{{}}}"'.format(var, var) for var in EXPECTED_ENV_VARS]
    )
    mqsub_cmd = "{python} {mqsub} -t 1 --hours 1 --no-email --no-executable-check -- bash -c '{job}'".format(
        python=sys.executable,
        mqsub=MQSUB,
        job=job_cmd,
    )

    child = pexpect.spawn(
        str(MQPIXI), [AVIARY_ENV],
        cwd=str(shared_cwd),
        encoding="utf-8",
    )
    child.expect(r'\$\s*', timeout=60)  # wait for shell to be ready
    child.sendline(mqsub_cmd)
    child.sendline("exit")
    child.expect(pexpect.EOF, timeout=JOB_TIMEOUT_SECONDS)
    output = child.before

    assert AVIARY_PIXI_BIN in output, (
        "expected aviary at {}\noutput:\n{}".format(AVIARY_PIXI_BIN, output)
    )
    for var, val in EXPECTED_ENV_VARS.items():
        assert "{}={}".format(var, val) in output, (
            "expected {}={}\noutput:\n{}".format(var, val, output)
        )
