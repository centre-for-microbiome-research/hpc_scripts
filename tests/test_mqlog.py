import getpass
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "bin" / "mqlog"
USER = getpass.getuser()


def write_fixtures(tmp_path, jobs):
    """Write a qstat -x listing and a qstat -xf JSON for the given jobs.

    jobs is a list of (job_id, exit_status, log_dir_or_None). When log_dir is
    given the job uses segregated logs (a directory containing <jobid>.OU/.ER),
    otherwise Output_Path/Error_Path point straight at files in tmp_path.
    """
    listing = []
    json_jobs = {}
    for job_id, exit_status, log_dir in jobs:
        listing.append(
            "{:<22} {:<16} {:<17} {:<8} {} {}".format(
                job_id, "somejob", USER, "00:01:00", "F", "cpu_batch_exec"))
        if log_dir is None:
            out = tmp_path / "{}.o".format(job_id)
            err = tmp_path / "{}.e".format(job_id)
        else:
            out = err = log_dir
        json_jobs[job_id] = {
            "Job_Name": "somejob",
            "job_state": "F",
            "Exit_status": exit_status,
            "Output_Path": "host:{}".format(out),
            "Error_Path": "host:{}".format(err),
        }

    x_file = tmp_path / "qstat_x.txt"
    x_file.write_text("\naqua:\nJob ID  Username\n------  --------\n"
                      + "\n".join(listing) + "\n")
    json_file = tmp_path / "qstat_xf.json"
    json_file.write_text(json.dumps({"Jobs": json_jobs}))
    return x_file, json_file


def write_logs(tmp_path, job_id, stdout_text, stderr_text, log_dir=None):
    if log_dir is None:
        (tmp_path / "{}.o".format(job_id)).write_text(stdout_text)
        (tmp_path / "{}.e".format(job_id)).write_text(stderr_text)
    else:
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "{}.OU".format(job_id)).write_text(stdout_text)
        (log_dir / "{}.ER".format(job_id)).write_text(stderr_text)


def run_mqlog(x_file, json_file, *extra):
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--no-pager",
         "--qstat-x-file", str(x_file), "--qstat-json-file", str(json_file)] + list(extra),
        text=True, capture_output=True)


def test_stdout_and_stderr_both_go_to_stdout(tmp_path):
    x_file, json_file = write_fixtures(tmp_path, [("100.aqua", 0, None)])
    write_logs(tmp_path, "100.aqua", "the-stdout\n", "the-stderr\n")

    result = run_mqlog(x_file, json_file)
    assert result.returncode == 0
    # Both streams are printed on stdout so one pager can show them together.
    assert "the-stdout" in result.stdout
    assert "the-stderr" in result.stdout
    assert "the-stdout" not in result.stderr
    assert "the-stderr" not in result.stderr
    # Headers separate the two when both are shown.
    assert "STDOUT" in result.stdout
    assert "STDERR" in result.stdout
    assert result.stdout.index("the-stdout") < result.stdout.index("the-stderr")


def test_dash_o_prints_only_stdout(tmp_path):
    x_file, json_file = write_fixtures(tmp_path, [("100.aqua", 0, None)])
    write_logs(tmp_path, "100.aqua", "the-stdout\n", "the-stderr\n")

    result = run_mqlog(x_file, json_file, "-o")
    assert result.returncode == 0
    assert result.stdout == "the-stdout\n"


def test_dash_e_prints_only_stderr(tmp_path):
    x_file, json_file = write_fixtures(tmp_path, [("100.aqua", 0, None)])
    write_logs(tmp_path, "100.aqua", "the-stdout\n", "the-stderr\n")

    result = run_mqlog(x_file, json_file, "-e")
    assert result.returncode == 0
    assert result.stdout == "the-stderr\n"


def test_defaults_to_most_recent_job_by_id(tmp_path):
    # Listed out of numeric order to check mqlog sorts rather than trusting qstat.
    x_file, json_file = write_fixtures(
        tmp_path, [("300.aqua", 0, None), ("100.aqua", 0, None), ("200.aqua", 0, None)])
    for job_id in ("100.aqua", "200.aqua", "300.aqua"):
        write_logs(tmp_path, job_id, "out-{}\n".format(job_id), "")

    result = run_mqlog(x_file, json_file, "-o")
    assert result.returncode == 0
    assert result.stdout == "out-300.aqua\n"


def test_dash_f_shows_most_recent_failed_job(tmp_path):
    x_file, json_file = write_fixtures(
        tmp_path,
        [("100.aqua", 1, None), ("200.aqua", 3, None), ("300.aqua", 0, None)])
    for job_id in ("100.aqua", "200.aqua", "300.aqua"):
        write_logs(tmp_path, job_id, "out-{}\n".format(job_id), "err-{}\n".format(job_id))

    result = run_mqlog(x_file, json_file, "-f", "-e")
    assert result.returncode == 0
    # 300 succeeded, so the newest *failed* job is 200.
    assert result.stdout == "err-200.aqua\n"
    assert "failed job 200.aqua" in result.stderr


def test_dash_f_errors_when_no_failed_jobs(tmp_path):
    x_file, json_file = write_fixtures(tmp_path, [("100.aqua", 0, None)])
    write_logs(tmp_path, "100.aqua", "out\n", "err\n")

    result = run_mqlog(x_file, json_file, "-f")
    assert result.returncode == 1
    assert "No failed jobs" in result.stderr


def test_dash_f_rejected_with_explicit_job_id(tmp_path):
    x_file, json_file = write_fixtures(tmp_path, [("100.aqua", 1, None)])
    result = run_mqlog(x_file, json_file, "-f", "100.aqua")
    assert result.returncode != 0
    assert "cannot be combined" in result.stderr


def test_skips_jobs_without_log_files(tmp_path):
    x_file, json_file = write_fixtures(
        tmp_path, [("100.aqua", 0, None), ("200.aqua", 0, None)])
    write_logs(tmp_path, "100.aqua", "out-100\n", "")  # 200 never wrote logs

    result = run_mqlog(x_file, json_file, "-o")
    assert result.returncode == 0
    assert result.stdout == "out-100\n"
    assert "200.aqua has no log file" in result.stderr


def test_segregated_log_directory(tmp_path):
    log_dir = tmp_path / "qsub_logs" / "somejob-1"
    x_file, json_file = write_fixtures(tmp_path, [("100.aqua", 0, log_dir)])
    write_logs(tmp_path, "100.aqua", "seg-out\n", "seg-err\n", log_dir=log_dir)

    result = run_mqlog(x_file, json_file, "-o")
    assert result.returncode == 0
    assert result.stdout == "seg-out\n"


def test_explicit_job_id(tmp_path):
    x_file, json_file = write_fixtures(
        tmp_path, [("100.aqua", 0, None), ("200.aqua", 0, None)])
    write_logs(tmp_path, "100.aqua", "out-100\n", "")
    write_logs(tmp_path, "200.aqua", "out-200\n", "")

    result = run_mqlog(x_file, json_file, "-o", "100.aqua")
    assert result.returncode == 0
    assert result.stdout == "out-100\n"

    # A bare numeric ID gets the .aqua suffix added.
    result = run_mqlog(x_file, json_file, "-o", "100")
    assert result.returncode == 0
    assert result.stdout == "out-100\n"


def test_explicit_unknown_job_id(tmp_path):
    x_file, json_file = write_fixtures(tmp_path, [("100.aqua", 0, None)])
    write_logs(tmp_path, "100.aqua", "out\n", "")

    result = run_mqlog(x_file, json_file, "999.aqua")
    assert result.returncode == 1
    assert "not found" in result.stderr


def test_no_finished_jobs(tmp_path):
    x_file, json_file = write_fixtures(tmp_path, [])
    result = run_mqlog(x_file, json_file)
    assert result.returncode == 1
    assert "No finished jobs found" in result.stderr


def test_pagination_used_on_a_tty(tmp_path):
    import pexpect

    x_file, json_file = write_fixtures(tmp_path, [("100.aqua", 0, None)])
    write_logs(tmp_path, "100.aqua", "paged-out\n", "paged-err\n")

    # PAGER records what it was handed, proving mqlog paginates by default when
    # stdout is a terminal, and that both streams arrive on the one pager.
    captured = tmp_path / "captured.txt"
    child = pexpect.spawn(
        sys.executable,
        [str(SCRIPT), "--qstat-x-file", str(x_file), "--qstat-json-file", str(json_file)],
        env={"PAGER": "cat > {}".format(captured), "PATH": "/usr/bin:/bin"},
        timeout=30,
    )
    child.expect(pexpect.EOF)
    child.close()
    assert child.exitstatus == 0

    text = captured.read_text()
    assert "paged-out" in text
    assert "paged-err" in text
