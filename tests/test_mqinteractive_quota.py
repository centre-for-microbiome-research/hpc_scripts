"""Tests for the mqinteractive interactive-queue quota warning.

All of these stub out qstat with files from tests/data, so they run anywhere.

# pixi run -e dev pytest tests/test_mqinteractive_quota.py
"""

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CHECKER = REPO / "bin" / "mqinteractive-quota-check"
DATA = REPO / "tests" / "data"


def check(queue, ncpus, mem_gb, ngpus=0, jobs_file=None, user="testuser"):
    return subprocess.run(
        [
            sys.executable,
            str(CHECKER),
            "--queue",
            queue,
            "--ncpus",
            str(ncpus),
            "--mem-gb",
            str(mem_gb),
            "--ngpus",
            str(ngpus),
            "--user",
            user,
            "--queue-file",
            str(DATA / ("qstat_Qf_%s.txt" % queue)),
            "--jobs-file",
            str(jobs_file or DATA / ("qstat_f_json_%s.json" % queue)),
        ],
        text=True,
        capture_output=True,
    )


def write_jobs(path, jobs):
    path.write_text(json.dumps({"Jobs": jobs}))
    return path


def running_cpu_job(ncpus, mem, ngpus=0, owner="testuser", queue="cpu_inter_exec"):
    return {
        "Job_Owner": "%s@aquarius01.ib0.hpc.qut.edu.au" % owner,
        "job_state": "R",
        "queue": queue,
        "Resource_List": {"mem": mem, "ncpus": ncpus, "ngpus": ngpus},
    }


def test_warns_when_cpus_and_ram_are_used_up():
    # Two 4 CPU / 16GB jobs already use the whole 8 CPU, 34GB per-user allowance.
    result = check("cpu_inter_exec", 8, 32)
    assert result.returncode == 1
    assert result.stdout == ""
    assert "does not fit within your quota" in result.stderr
    assert "CPUs: 8 already in use" in result.stderr
    assert "+ 8 requested = 16, over your limit of 8" in result.stderr
    assert "RAM: 32GB already in use" in result.stderr
    assert "+ 32GB requested = 64GB, over your limit of 34GB" in result.stderr
    # The existing jobs are named so it is obvious what is holding the quota.
    assert "25226997 (R, 4 CPUs, 16GB)" in result.stderr
    assert "25230242 (R, 4 CPUs, 16GB)" in result.stderr
    # ... and another user's job in the same queue is not counted as ours.
    assert "25230999" not in result.stderr


def test_no_warning_when_nothing_running(tmp_path):
    result = check("cpu_inter_exec", 8, 32, jobs_file=write_jobs(tmp_path / "jobs.json", {}))
    assert result.returncode == 0
    assert result.stderr == ""


def test_half_still_warns_but_is_not_suggested(tmp_path):
    result = check("cpu_inter_exec", 4, 16)
    assert result.returncode == 1
    assert "+ 4 requested = 12, over your limit of 8" in result.stderr
    assert "mqinteractive half" not in result.stderr
    # Asking for the full size does suggest halving.
    assert "mqinteractive half" in check("cpu_inter_exec", 8, 32).stderr


def test_gpu_uses_the_per_user_limit_override():
    # gpu_inter_exec allows 2 GPUs generally but only 1 to testuser, who has 1 running.
    result = check("gpu_inter_exec", 8, 32, ngpus=1)
    assert result.returncode == 1
    assert "GPUs: 1 already in use" in result.stderr
    assert "+ 1 requested = 2, over your limit of 1" in result.stderr


def test_gpu_within_generic_limit_is_fine_for_another_user(tmp_path):
    result = check("gpu_inter_exec", 8, 32, ngpus=1, user="othertestuser")
    assert result.returncode == 0, result.stderr


def test_warns_when_job_count_is_used_up(tmp_path):
    jobs = {
        "252310%02d.aqua" % i: running_cpu_job(1, "1gb") for i in range(8)
    }
    result = check(
        "cpu_inter_exec", 1, 1, jobs_file=write_jobs(tmp_path / "jobs.json", jobs)
    )
    assert result.returncode == 1
    assert "you already have 8 job(s) running in cpu_inter_exec" in result.stderr


def test_queued_jobs_do_not_hold_the_running_allowance(tmp_path):
    queued = running_cpu_job(8, "32gb")
    queued["job_state"] = "Q"
    jobs = {"25231001.aqua": queued}
    result = check(
        "cpu_inter_exec", 8, 32, jobs_file=write_jobs(tmp_path / "jobs.json", jobs)
    )
    # Nothing of the user's is running, so the CPU/RAM allowance is untouched.
    assert result.returncode == 0, result.stderr


def test_request_larger_than_a_single_job_may_be_is_refused(tmp_path):
    result = check(
        "cpu_inter_exec", 16, 64, jobs_file=write_jobs(tmp_path / "jobs.json", {})
    )
    assert result.returncode == 1
    assert "a single cpu_inter_exec job may not exceed 8" in result.stderr
    assert "PBS will refuse this submission" in result.stderr


def test_broken_qstat_output_does_not_block_submission(tmp_path):
    junk = tmp_path / "junk.json"
    junk.write_text("not json at all")
    result = check("cpu_inter_exec", 8, 32, jobs_file=junk)
    assert result.returncode == 0
    assert result.stderr == ""


def run_mqinteractive(tmp_path, *args):
    """Run real-mqinteractive with fake qsub/qstat, returning (result, qsub_log)."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()

    qsub_log = tmp_path / "qsub.args"
    qsub = fake_bin / "qsub"
    qsub.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$*" > "$QSUB_LOG"\necho fake qsub ran\n'
    )

    # Serve the tests/data fixtures in place of the real queue and job listing.
    qstat = fake_bin / "qstat"
    qstat.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "-Qf" ]; then cat "%s"; else cat "%s"; fi\n'
        % (
            DATA / "qstat_Qf_cpu_inter_exec.txt",
            DATA / "qstat_f_json_cpu_inter_exec.json",
        )
    )
    for script in (qsub, qstat):
        script.chmod(script.stat().st_mode | stat.S_IEXEC)

    home = tmp_path / "home"
    home.mkdir()
    env = dict(os.environ)
    env.update(
        HOME=str(home),
        USER="testuser",
        QSUB_LOG=str(qsub_log),
        PATH="%s:%s" % (fake_bin, env["PATH"]),
    )
    result = subprocess.run(
        ["bash", str(REPO / "bin" / "real-mqinteractive")] + list(args),
        text=True,
        capture_output=True,
        env=env,
        cwd=str(tmp_path),
    )
    return result, qsub_log


def test_real_mqinteractive_warns_and_still_submits(tmp_path):
    """The warning reaches the user, and qsub is run regardless."""
    result, qsub_log = run_mqinteractive(tmp_path)
    assert "does not fit within your quota" in result.stderr
    assert "fake qsub ran" in result.stdout
    assert "select=1:ncpus=8:mem=32GB" in qsub_log.read_text()


def test_attach_is_gone(tmp_path):
    # The admins disallowed ssh-ing into a running interactive job, so "mqinteractive a"
    # says so rather than silently starting a session.
    result, qsub_log = run_mqinteractive(tmp_path, "a")
    assert result.returncode == 1
    assert "no longer allowed" in result.stderr
    assert not qsub_log.exists()
