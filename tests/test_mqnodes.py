import importlib.machinery
import importlib.util
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "bin" / "mqnodes"
QSTAT_JSON = REPO / "tests" / "data" / "qstat_mqnodes.json"


def load_mqnodes():
    loader = importlib.machinery.SourceFileLoader("mqnodes", str(SCRIPT))
    spec = importlib.util.spec_from_loader("mqnodes", loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules["mqnodes"] = module  # dataclasses look the module up by name
    loader.exec_module(module)
    return module


mq = load_mqnodes()
mq.Colors.disable()  # colour codes would throw off the column checks


def run(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *args],
                          text=True, capture_output=True)


def write_log(folder, name, text, modified):
    log_dir = folder / ".snakemake" / "log"
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / name
    path.write_text(text)
    os.utime(path, (modified, modified))


def clear_caches():
    mq.find_repo_root.cache_clear()
    mq.snakemake_submitted_jobs.cache_clear()


# ---------------------------------------------------------------- reading node output

def test_parse_node_output_reads_every_kind_of_line():
    text = "\n".join([
        "load\t3.5\t40",
        "mem\t1000\t250",
        "users\t7",
        "session\tmy session\t1761000000\t2",
        "session\tbroken\tnot-a-number\t0",
        "exited\told-one",
        "pane\tmy session\t/home/someone/repo",
        "warning\tzellij not found",
    ])
    r = mq.parse_node_output("aquarius01", text)
    assert (r.load1, r.ncpu, r.mem_total_kb, r.mem_avail_kb, r.users) == (3.5, 40, 1000, 250, 7)
    assert r.sessions == (mq.Session("my session", 1761000000, 2),)
    assert r.exited == ("old-one",)
    assert r.folders("my session") == ["/home/someone/repo"]
    assert r.warning == "zellij not found"


def test_same_session_from_two_zellij_copies_is_listed_once():
    r = mq.parse_node_output("aquarius01", "session\tdup\t10\t0\nsession\tdup\t10\t0")
    assert [s.name for s in r.sessions] == ["dup"]


def test_panes_in_the_same_folder_become_one_row():
    text = "\n".join([
        "session\tcowbell\t0\t0",
        "pane\tcowbell\t/w/aviary-polish-fix",
        "pane\tcowbell\t/w/aviary-polish-fix",
        "pane\tcowbell\t/w/sandpiper/backend",
        "pane\tother\t/w/aviary-polish-fix",  # same folder in another session stays separate
    ])
    r = mq.parse_node_output("aquarius04", text)
    assert r.folders("cowbell") == ["/w/aviary-polish-fix", "/w/sandpiper/backend"]
    assert r.folders("other") == ["/w/aviary-polish-fix"]


def test_parse_jobs_json_reads_state_submit_host_and_time():
    jobs = mq.parse_jobs_json(QSTAT_JSON.read_text())
    assert [(j.job_id, j.state, j.submit_host, j.kind) for j in jobs] == [
        ("100.aqua", "R", "aquarius02", "running"),
        ("101.aqua", "Q", "aquarius03", "queued"),
        ("102.aqua", "H", "aquarius03", "queued"),        # held counts as queued
        ("103.aqua", "R", "aquarius02", "interactive")]  # qsub -I job


def test_parse_pbs_time():
    assert mq.parse_pbs_time("Wed Oct 21 15:35:43 2026") > 0
    assert mq.parse_pbs_time(None) == 0.0
    assert mq.parse_pbs_time("garbage") == 0.0


# ---------------------------------------------------------------- folders and repos

def test_describe_folder_uses_nearest_pixi_project(tmp_path):
    repo = tmp_path / "aviary-v0.13.14"
    (repo / "runs" / "sample1").mkdir(parents=True)
    (repo / "pixi.toml").write_text("")
    plain = tmp_path / "nothing"
    plain.mkdir()
    clear_caches()
    assert mq.describe_folder(str(repo / "runs" / "sample1")) == "aviary-v0.13.14/runs/sample1"
    assert mq.describe_folder(str(plain)) == str(plain)


def test_git_checkout_wins_over_a_pixi_project_inside_it(tmp_path):
    checkout = tmp_path / "singlem-myversion"
    nested = checkout / "extras" / "update_metapackage"
    nested.mkdir(parents=True)
    (checkout / ".git").mkdir()
    (nested / "pixi.toml").write_text("")
    clear_caches()
    assert mq.describe_folder(str(nested)) == "singlem-myversion/extras/update_metapackage"
    assert mq.describe_folder(str(checkout)) == "singlem-myversion"


def test_a_home_folder_is_never_treated_as_a_repo(tmp_path):
    home = tmp_path / "home" / "someone"
    (home / ".git").mkdir(parents=True)
    (home / "work").mkdir()
    clear_caches()
    assert mq.find_repo_root(str(home / "work")) == ""


# ---------------------------------------------------------------- matching jobs to panes

def test_only_logs_written_since_the_oldest_job_are_read(tmp_path):
    write_log(tmp_path, "old.snakemake.log", "Submitted job 1 with external jobid '1.aqua'.", 1000)
    write_log(tmp_path, "a.snakemake.log", "Submitted job 2 with external jobid '2.aqua'.", 5000)
    write_log(tmp_path, "b.snakemake.log", "Submitted job 3 with external jobid '3.aqua'.\nnoise", 6000)
    clear_caches()
    assert mq.snakemake_submitted_jobs(str(tmp_path), 4000.0) == {"2.aqua", "3.aqua"}
    assert mq.snakemake_submitted_jobs(str(tmp_path / "missing"), 0.0) == frozenset()


def test_jobs_are_traced_to_their_pane_and_counted_in_the_table(tmp_path):
    run_dir = tmp_path / "myrepo"
    (run_dir / ".git").mkdir(parents=True)
    write_log(run_dir, "x.snakemake.log",
              "external jobid '10.aqua'\nexternal jobid '11.aqua'\nexternal jobid '99.aqua'", 5000)
    jobs = [mq.Job("10.aqua", "R", "aquarius04", 4900.0),
            mq.Job("11.aqua", "Q", "aquarius04", 4950.0),
            mq.Job("12.aqua", "Q", "aquarius04", 4990.0)]
    reports = [mq.NodeReport("aquarius04", sessions=(mq.Session("cowbell", 0, 0),),
                             panes=(("cowbell", str(run_dir)),))]
    clear_caches()

    origins = mq.find_job_origins(reports, jobs)

    assert set(origins) == {"10.aqua", "11.aqua"}  # 99 has finished, 12 came from somewhere else
    assert origins["10.aqua"] == mq.JobOrigin("aquarius04", "cowbell", str(run_dir))
    out = mq.render_zellij(reports, "elsewhere", origins, jobs)
    header = next(l for l in out if l.startswith("Session") and "Pane" in l)
    assert header.split() == ["Session", "Panel", "folder", "Run", "Queue"]
    row = next(l for l in out if "myrepo" in l)
    assert row.split() == ["cowbell", "myrepo", "1", "1"]


# ---------------------------------------------------------------- printing

def test_session_with_no_panes_found_still_gets_a_row():
    reports = [mq.NodeReport("aquarius02", sessions=(mq.Session("fresh", 0, 0),))]
    row = next(l for l in mq.render_zellij(reports, "elsewhere") if l.startswith("fresh"))
    assert row.split() == ["fresh", "?", "-", "-"]


def test_exited_sessions_are_counted_once_and_exclude_live_ones():
    reports = [
        mq.NodeReport("aquarius01", exited=("a", "b")),
        mq.NodeReport("aquarius02", sessions=(mq.Session("b", 0, 0),), exited=("a", "c")),
    ]
    out = "\n".join(mq.render_zellij(reports, "elsewhere"))
    assert "2 exited sessions can be brought back" in out
    assert "aquarius02  b" in out


def test_zellij_warning_is_shown():
    out = "\n".join(mq.render_zellij([mq.NodeReport("aquarius01", warning="zellij not found")], "x"))
    assert "aquarius01: zellij not found" in out


def test_unreachable_node_is_reported_not_fatal():
    out = "\n".join(mq.render_nodes([mq.NodeReport("aquarius09", error="unreachable (x)")], "x"))
    assert "aquarius09" in out and "unreachable (x)" in out


def test_job_counts_by_submit_host():
    result = run("--no-nodes", "--qstat-json-file", str(QSTAT_JSON))
    assert result.returncode == 0, result.stderr
    assert "Jobs: 1 interactive, 1 running, 2 queued  (4 total)" in result.stdout
    lines = result.stdout.splitlines()
    assert any(l.split() == ["Submitted", "from", "Int", "Run", "Queue"] for l in lines)
    assert any(l.split() == ["aquarius02", "1", "1", "0"] for l in lines)
    assert any(l.split() == ["aquarius03", "0", "0", "2"] for l in lines)


# ---------------------------------------------------------------- the script that runs on each node

def test_node_script_reports_live_pane_folders(tmp_path):
    """Start a fake `zellij --server` with a child shell and check its folder is reported."""
    fake_zellij = tmp_path / "zellij"
    fake_zellij.write_text("#!/bin/bash\n(cd \"$PANE_DIR\" && exec sleep 30) & wait\n")
    fake_zellij.chmod(0o755)
    pane_dir = tmp_path / "pane here"
    pane_dir.mkdir()
    server = subprocess.Popen([str(fake_zellij), "--server", str(tmp_path / "sock" / "probe-session")],
                              env={**os.environ, "PANE_DIR": str(pane_dir)})
    try:
        time.sleep(0.5)
        out = subprocess.run(["bash", "-s"], input=mq.NODE_SCRIPT, text=True,
                             capture_output=True, timeout=30).stdout
    finally:
        server.kill()
        subprocess.run(["pkill", "-P", str(server.pid)])
    assert f"pane\tprobe-session\t{pane_dir}" in out.splitlines()


def test_sessions_in_the_pane_table_are_separated_by_a_line():
    reports = [mq.NodeReport("aquarius02", sessions=(mq.Session("one", 0, 0), mq.Session("two", 0, 0)),
                             panes=(("one", "/a"), ("one", "/b"), ("two", "/c")))]
    out = mq.render_pane_table(reports, {}, ())
    rows = [l.split()[0] if l.split() else "" for l in out]
    # header, rule, one/a, (continuation)/b, separator, two/c
    assert rows[2] == "one" and out[3].split()[0] == "/b"
    assert set(out[4].replace(" ", "")) == {"-"}
    assert rows[5] == "two"
    assert sum(1 for l in out if set(l.replace(" ", "")) == {"-"}) == 2  # header rule + one separator
