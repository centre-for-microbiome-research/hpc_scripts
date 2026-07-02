"""Integration tests for mpixi.

Submits real PBS jobs via mqsub and waits for them to complete, so each test
takes several minutes plus queue time. Skipped automatically when qsub is not
available on PATH.

# pixi run -e dev pytest tests/test_mpixi.py
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pexpect
import pytest

REPO = Path(__file__).resolve().parents[1]
MQSUB = REPO / "bin" / "mqsub"
MPIXI = REPO / "bin" / "mpixi"

AVIARY_ENV = "aviary-v0-13-0"
_PIXI_TOML = Path("/pkg/cmr/mpixi/pixi.toml")
AVIARY_PIXI_BIN = str(_PIXI_TOML.resolve().parent / ".pixi" / "envs" / AVIARY_ENV / "bin" / "aviary")

EXPECTED_ENV_VARS = {
    "GTDBTK_DATA_PATH": "/work/microbiome/db/gtdb/gtdb_release232/auxillary_files/gtdbtk_package/full_package/release232",
    "CHECKM2DB": "/work/microbiome/db/CheckM2_database/uniref100.KO.1.dmnd",
    "EGGNOG_DATA_DIR": "/mnt/hpccs01/work/microbiome/db/eggnog-mapper/2.1.3",
    "SINGLEM_METAPACKAGE_PATH": "/work/microbiome/db/singlem/S6.5.0.GTDB_r232.metapackage_20260319.smpkg.zb",
    "METABULI_DB_PATH": "/work/microbiome/db/metabuli/2024-3-28-GTDB214.1+humanT2T",
}

JOB_TIMEOUT_SECONDS = 30 * 60

pytestmark = pytest.mark.skipif(
    shutil.which("qsub") is None,
    reason="qsub not available; mpixi integration tests need a PBS queue",
)


@pytest.fixture
def shared_cwd():
    # PBS writes the job's .o/.e files to the submission cwd on the compute
    # node; if cwd is /tmp it won't be visible to the head node. Use $HOME.
    base = Path.home() / ".mpixi_pytest"
    base.mkdir(exist_ok=True)
    path = Path(tempfile.mkdtemp(dir=str(base)))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def test_mpixi_aviary_env(shared_cwd):
    """Shell into aviary-v0-13-0 via mpixi, submit a job that checks binary path and env vars."""
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
        str(MPIXI), [AVIARY_ENV],
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
