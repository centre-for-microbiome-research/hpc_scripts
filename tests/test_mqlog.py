import getpass
import io
import json
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "bin" / "mqlog"
USER = getpass.getuser()


def write_fixtures(tmp_path, jobs, completion_times=None):
    """Write a qstat -x listing and a qstat -xf JSON for the given jobs.

    jobs is a list of (job_id, exit_status, log_dir_or_None). When log_dir is
    given the job uses segregated logs (a directory containing <jobid>.OU/.ER),
    otherwise Output_Path/Error_Path point straight at files in tmp_path.
    """
    completion_times = completion_times or {}
    listing = []
    json_jobs = {}
    for job_id, exit_status, log_dir in jobs:
        numeric_id = int(job_id.split('.')[0].split('[', 1)[0])
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
            "obittime": completion_times.get(
                job_id, (datetime(2024, 1, 1) + timedelta(seconds=numeric_id)).strftime(
                    "%a %b %d %H:%M:%S %Y")),
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


def test_defaults_to_most_recent_job_by_completion_time(tmp_path):
    # 200 finished last even though 300 was submitted later.
    x_file, json_file = write_fixtures(
        tmp_path, [("300.aqua", 0, None), ("100.aqua", 0, None), ("200.aqua", 0, None)],
        completion_times={
            "100.aqua": "Mon Jan  1 01:00:00 2024",
            "300.aqua": "Mon Jan  1 02:00:00 2024",
            "200.aqua": "Mon Jan  1 03:00:00 2024",
        })
    for job_id in ("100.aqua", "200.aqua", "300.aqua"):
        write_logs(tmp_path, job_id, "out-{}\n".format(job_id), "")

    result = run_mqlog(x_file, json_file, "-o")
    assert result.returncode == 0
    assert result.stdout == "out-200.aqua\n"


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


def test_array_log_placeholder_uses_concrete_subjob_id(tmp_path):
    job_id = "100[7].aqua"
    x_file, json_file = write_fixtures(tmp_path, [(job_id, 0, None)])
    data = json.loads(json_file.read_text())
    data["Jobs"][job_id]["Output_Path"] = "host:{}".format(tmp_path / "100[].aqua.OU")
    data["Jobs"][job_id]["Error_Path"] = "host:{}".format(tmp_path / "100[].aqua.ER")
    json_file.write_text(json.dumps(data))
    (tmp_path / "{}.OU".format(job_id)).write_text("array-out\n")
    (tmp_path / "{}.ER".format(job_id)).write_text("array-err\n")

    result = run_mqlog(x_file, json_file, "-o")
    assert result.returncode == 0
    assert result.stdout == "array-out\n"


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


def spawn_on_tty(tmp_path, x_file, json_file, pager, *extra):
    """Run mqlog under a pty (so it paginates) with PAGER set, return its output."""
    import pexpect

    env = {"PATH": "/usr/bin:/bin", "TERM": "xterm", "USER": USER}
    if pager is not None:
        env["PAGER"] = pager
    child = pexpect.spawn(
        sys.executable,
        [str(SCRIPT), "--qstat-x-file", str(x_file), "--qstat-json-file", str(json_file)]
        + list(extra),
        env=env, timeout=30,
    )
    child.expect(pexpect.EOF)
    output = child.before.decode()
    child.close()
    return child.exitstatus, output


SMCUP = "\x1b[?1049h"  # switch to the alternate screen
RMCUP = "\x1b[?1049l"  # switch back, discarding what was shown on it


def test_long_log_leaves_nothing_on_screen_after_quitting(tmp_path):
    """Quitting the pager on a long log must not strand it on the terminal.

    This is what `less -X` breaks: it suppresses the terminal init/deinit strings,
    so less never switches to the alternate screen and the log stays behind.
    """
    import pexpect

    x_file, json_file = write_fixtures(tmp_path, [("100.aqua", 0, None)])
    write_logs(tmp_path, "100.aqua", "out-line\n" * 400, "err-line\n")

    log = io.BytesIO()
    child = pexpect.spawn(
        sys.executable,
        [str(SCRIPT), "--qstat-x-file", str(x_file), "--qstat-json-file", str(json_file)],
        env={"PATH": "/usr/bin:/bin", "TERM": "xterm", "LESS": "", "USER": USER},
        timeout=30,
    )
    # logfile_read captures everything, including bytes expect() consumes.
    child.logfile_read = log
    child.expect("out-line")
    child.send("q")
    child.expect(pexpect.EOF)
    child.close()

    raw = log.getvalue().decode(errors="replace")
    assert SMCUP in raw, "pager did not use the alternate screen"
    # What survives on the main screen is whatever was written before switching to
    # the alternate screen, plus anything after switching back.
    main_screen = raw.split(SMCUP)[0] + raw.split(RMCUP)[-1]
    assert "out-line" not in main_screen


def test_short_log_stays_on_screen(tmp_path):
    # The counterpart: a log that fits one screen must print inline and stay put
    # (less -F), not flash up on the alternate screen and vanish on exit.
    import pexpect

    x_file, json_file = write_fixtures(tmp_path, [("100.aqua", 0, None)])
    write_logs(tmp_path, "100.aqua", "short-out\n", "short-err\n")

    log = io.BytesIO()
    child = pexpect.spawn(
        sys.executable,
        [str(SCRIPT), "--qstat-x-file", str(x_file), "--qstat-json-file", str(json_file)],
        env={"PATH": "/usr/bin:/bin", "TERM": "xterm", "LESS": "", "USER": USER},
        timeout=30,
    )
    child.logfile_read = log
    child.expect(pexpect.EOF)
    child.close()

    raw = log.getvalue().decode(errors="replace")
    assert SMCUP not in raw, "short log should not use the alternate screen"
    assert "short-out" in raw
    assert "short-err" in raw


def test_unrunnable_pager_still_prints_the_logs(tmp_path):
    # bash starts fine and only the pager inside it fails, so mqlog has to notice
    # the 127 and re-emit; otherwise the logs are silently lost with exit 0.
    x_file, json_file = write_fixtures(tmp_path, [("100.aqua", 0, None)])
    write_logs(tmp_path, "100.aqua", "the-stdout\n", "the-stderr\n")

    status, output = spawn_on_tty(tmp_path, x_file, json_file, "definitely-not-a-real-pager")
    assert status == 0
    assert "the-stdout" in output
    assert "the-stderr" in output
    assert "could not be run" in output


def test_empty_pager_means_no_pager(tmp_path):
    x_file, json_file = write_fixtures(tmp_path, [("100.aqua", 0, None)])
    write_logs(tmp_path, "100.aqua", "the-stdout\n", "the-stderr\n")

    status, output = spawn_on_tty(tmp_path, x_file, json_file, "")
    assert status == 0
    assert "the-stdout" in output
    # No pager was attempted, so there is nothing to warn about.
    assert "could not be run" not in output


def test_pager_exiting_nonzero_does_not_double_print(tmp_path):
    # A pager that ran but exited non-zero (e.g. the reader quit) already showed
    # the output; only 126/127 mean it never ran at all.
    x_file, json_file = write_fixtures(tmp_path, [("100.aqua", 0, None)])
    write_logs(tmp_path, "100.aqua", "the-stdout\n", "the-stderr\n")

    status, output = spawn_on_tty(tmp_path, x_file, json_file, "cat; exit 3")
    assert status == 0
    assert output.count("the-stdout") == 1
    assert "could not be run" not in output


def test_broken_pipe_is_silent(tmp_path):
    # `mqlog -o | head -1` must not spew a BrokenPipeError traceback.
    x_file, json_file = write_fixtures(tmp_path, [("100.aqua", 0, None)])
    write_logs(tmp_path, "100.aqua", "out-line\n" * 5000, "")

    proc = subprocess.run(
        "{} {} --no-pager -o --qstat-x-file {} --qstat-json-file {} | head -1".format(
            sys.executable, SCRIPT, x_file, json_file),
        shell=True, text=True, capture_output=True)
    assert proc.stdout == "out-line\n"
    assert "BrokenPipeError" not in proc.stderr
    assert "Exception ignored" not in proc.stderr
