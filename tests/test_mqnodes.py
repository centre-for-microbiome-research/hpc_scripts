import importlib.machinery
import importlib.util
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

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


def zellij(name, clients=0):
    return mq.Session("zellij", name, 0, clients)


def tmux(name, clients=0):
    return mq.Session("tmux", name, 0, clients)


# ---------------------------------------------------------------- reading node output

def test_parse_node_output_reads_every_kind_of_line():
    text = "\n".join([
        "load\t3.5\t40",
        "mem\t1000\t250",
        "users\t7",
        "session\tzellij\tmy session\t1761000000\t2",
        "session\tzellij\tbroken\tnot-a-number\t0",
        "session\ttmux\t0\t1761000100\t1",
        "exited\told-one",
        "pane\tzellij\tmy session\t/home/someone/repo",
        "pane\ttmux\t0\t/home/someone/other:with-colon",
        "snakemake\t/home/someone/repo",
        "warning\tzellij not found",
    ])
    r = mq.parse_node_output("aquarius01", text)
    assert (r.load1, r.ncpu, r.mem_total_kb, r.mem_avail_kb, r.users) == (3.5, 40, 1000, 250, 7)
    assert r.sessions == (mq.Session("zellij", "my session", 1761000000, 2, "zellij"),
                          mq.Session("tmux", "0", 1761000100, 1, "tmux"))
    assert r.exited == ("old-one",)
    assert r.folders(r.sessions[0]) == ["/home/someone/repo"]
    assert r.folders(r.sessions[1]) == ["/home/someone/other:with-colon"]
    assert r.snakemake_folders == {"/home/someone/repo"}
    assert r.warning == "zellij not found"


def test_same_session_from_two_zellij_copies_is_listed_once():
    r = mq.parse_node_output("aquarius01", "session\tzellij\tdup\t10\t0\nsession\tzellij\tdup\t10\t0")
    assert [s.name for s in r.sessions] == ["dup"]


def test_zellij_and_tmux_sessions_with_the_same_name_stay_separate():
    text = "session\tzellij\twork\t10\t0\nsession\ttmux\twork\t10\t0\npane\ttmux\twork\t/t"
    r = mq.parse_node_output("aquarius01", text)
    assert [(s.tool, s.name) for s in r.sessions] == [("zellij", "work"), ("tmux", "work")]
    assert r.folders(r.sessions[0]) == [] and r.folders(r.sessions[1]) == ["/t"]


def test_panes_in_the_same_folder_become_one_row():
    text = "\n".join([
        "session\tzellij\tcowbell\t0\t0",
        "pane\tzellij\tcowbell\t/w/aviary-polish-fix",
        "pane\tzellij\tcowbell\t/w/aviary-polish-fix",
        "pane\tzellij\tcowbell\t/w/sandpiper/backend",
        "pane\tzellij\tother\t/w/aviary-polish-fix",  # same folder in another session stays separate
    ])
    r = mq.parse_node_output("aquarius04", text)
    assert r.folders(zellij("cowbell")) == ["/w/aviary-polish-fix", "/w/sandpiper/backend"]
    assert r.folders(zellij("other")) == ["/w/aviary-polish-fix"]


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


def test_your_home_folder_is_never_treated_as_a_repo(tmp_path, monkeypatch):
    home = tmp_path / "users" / "someone"  # not under a folder called "home"
    (home / ".git").mkdir(parents=True)
    (home / "work").mkdir()
    monkeypatch.setenv("HOME", str(home))
    clear_caches()
    assert mq.find_repo_root(str(home / "work")) == ""
    assert mq.find_repo_root(str(home)) == ""


def test_a_repo_below_a_folder_called_home_is_still_found(tmp_path, monkeypatch):
    repo = tmp_path / "scratch" / "home" / "project"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path / "elsewhere"))
    clear_caches()
    assert mq.find_repo_root(str(repo)) == str(repo)


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
    reports = [mq.NodeReport("aquarius04", sessions=(tmux("cowbell"),),
                             panes=(("tmux", "cowbell", str(run_dir)),))]
    clear_caches()

    origins = mq.find_job_origins(reports, jobs)

    assert set(origins) == {"10.aqua", "11.aqua"}  # 99 has finished, 12 came from somewhere else
    assert origins["10.aqua"] == mq.JobOrigin("aquarius04", "tmux", "cowbell", str(run_dir))
    out = mq.render_sessions(reports, "elsewhere", origins, jobs)
    header = next(l for l in out if l.startswith("Session") and "Panel" in l)
    assert header.split() == ["Session", "Panel", "folder", "Run", "Queue"]
    row = next(l for l in out if "myrepo" in l)
    assert row.split() == ["cowbell", "(tmux)", "myrepo", "1", "1"]


def test_shared_folder_jobs_go_to_the_node_where_snakemake_is_running(tmp_path):
    run_dir = tmp_path / "shared"
    run_dir.mkdir()
    write_log(run_dir, "x.snakemake.log", "external jobid '10.aqua'", 5000)
    jobs = [mq.Job("10.aqua", "Q", "aquarius04", 4900.0)]
    idle = mq.NodeReport("aquarius02", sessions=(tmux("probe"),),
                         panes=(("tmux", "probe", str(run_dir)),))
    running = mq.NodeReport("aquarius04", sessions=(zellij("cowbell"),),
                            panes=(("zellij", "cowbell", str(run_dir)),),
                            snakemake_folders=frozenset({str(run_dir)}))
    clear_caches()
    # aquarius02 comes first, but snakemake is running on aquarius04
    assert mq.find_job_origins([idle, running], jobs)["10.aqua"].node == "aquarius04"
    # with no snakemake running anywhere it falls back to the first pane
    assert mq.find_job_origins([idle, mq.NodeReport("aquarius04", panes=running.panes)],
                               jobs)["10.aqua"].node == "aquarius02"


# ---------------------------------------------------------------- printing

def test_pane_table_labels_every_session_with_its_tool():
    reports = [mq.NodeReport("aquarius02", sessions=(zellij("zz"), tmux("tt")),
                             panes=(("zellij", "zz", "/a"), ("tmux", "tt", "/b")))]
    labels = [l.split()[:2] for l in mq.render_pane_table(reports, {}, ()) if l.split()[0] in ("zz", "tt")]
    assert labels == [["zz", "(zellij)"], ["tt", "(tmux)"]]


def test_session_table_shows_the_tool():
    reports = [mq.NodeReport("aquarius02", sessions=(zellij("zz"), tmux("tt", clients=1)))]
    out = mq.render_sessions(reports, "elsewhere")
    assert next(l for l in out if " zz " in l).split()[:3] == ["aquarius02", "zellij", "zz"]
    assert next(l for l in out if " tt " in l).split()[:3] == ["aquarius02", "tmux", "tt"]
    text = "\n".join(out)
    assert "Attach (zellij): ssh -t <node>.qut.edu.au zellij attach <session>" in text
    assert "Attach (tmux): ssh -t <node>.qut.edu.au tmux attach -t <session>" in text


def test_only_the_attach_hint_for_tools_in_use_is_shown():
    text = "\n".join(mq.render_sessions([mq.NodeReport("aquarius02", sessions=(tmux("tt"),))], "x"))
    assert "Attach (tmux)" in text and "Attach (zellij)" not in text


def test_current_session_is_marked_per_tool():
    sessions = [("aquarius01", zellij("work")), ("aquarius01", tmux("work"))]
    out = mq.render_session_table(sessions, "aquarius01", {"tmux": "work"})
    assert out[2].split()[-1] == "detached" and out[3].split()[-1] == "current"


def test_session_with_no_panes_found_still_gets_a_row():
    reports = [mq.NodeReport("aquarius02", sessions=(zellij("fresh"),))]
    row = next(l for l in mq.render_sessions(reports, "elsewhere") if l.startswith("fresh"))
    assert row.split() == ["fresh", "(zellij)", "?", "-", "-"]


def test_exited_sessions_are_counted_once_and_exclude_live_ones():
    reports = [
        mq.NodeReport("aquarius01", exited=("a", "b")),
        mq.NodeReport("aquarius02", sessions=(zellij("b"),), exited=("a", "c")),
    ]
    out = "\n".join(mq.render_sessions(reports, "elsewhere"))
    assert "2 exited zellij sessions can be brought back" in out
    assert "aquarius02  zellij b" in out


def test_zellij_warning_is_shown():
    out = "\n".join(mq.render_sessions([mq.NodeReport("aquarius01", warning="zellij not found")], "x"))
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


def test_sessions_in_the_pane_table_are_separated_by_a_line():
    reports = [mq.NodeReport("aquarius02", sessions=(zellij("one"), zellij("two")),
                             panes=(("zellij", "one", "/a"), ("zellij", "one", "/b"),
                                    ("zellij", "two", "/c")))]
    out = mq.render_pane_table(reports, {}, ())
    rows = [l.split()[0] if l.split() else "" for l in out]
    # header, rule, one/a, (continuation)/b, separator, two/c
    assert out[2].split()[:2] == ["one", "(zellij)"] and out[3].split()[0] == "/b"
    assert set(out[4].replace(" ", "")) == {"-"}
    assert out[5].split()[:2] == ["two", "(zellij)"]
    assert sum(1 for l in out if set(l.replace(" ", "")) == {"-"}) == 2  # header rule + one separator


# ---------------------------------------------------------------- the script that runs on each node

def test_node_script_reports_live_zellij_pane_folders(tmp_path):
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
    assert f"pane\tzellij\tprobe-session\t{pane_dir}" in out.splitlines()


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")
def test_node_script_reports_tmux_sessions_and_pane_folders(tmp_path):
    """Start real tmux servers (default and -L named) in a private TMUX_TMPDIR."""
    first, second = tmp_path / "first dir", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    env = {**os.environ, "TMUX_TMPDIR": str(tmp_path)}
    env.pop("TMUX", None)
    subprocess.run(["tmux", "new-session", "-d", "-s", "probe", "-c", str(first)], env=env, check=True)
    subprocess.run(["tmux", "split-window", "-t", "probe", "-c", str(second)], env=env, check=True)
    subprocess.run(["tmux", "-L", "named", "new-session", "-d", "-s", "other", "-c", str(first)],
                   env=env, check=True)
    try:
        time.sleep(0.5)
        out = subprocess.run(["bash", "-s"], input=mq.NODE_SCRIPT, text=True, env=env,
                             capture_output=True, timeout=30).stdout.splitlines()
    finally:
        subprocess.run(["tmux", "kill-server"], env=env)
        subprocess.run(["tmux", "-L", "named", "kill-server"], env=env)
    assert any(l.startswith("session\ttmux\tprobe\t") and l.endswith("\t0\ttmux") for l in out)
    assert any(l.startswith("session\ttmux\tother [-L named]\t") for l in out)
    assert f"pane\ttmux\tprobe\t{first}" in out
    assert f"pane\ttmux\tprobe\t{second}" in out
    assert f"pane\ttmux\tother [-L named]\t{first}" in out


# ---------------------------------------------------------------- when ssh to another node fails

def test_ssh_failures_are_explained_in_plain_words():
    no_key = mq.explain_ssh_failure(
        "aquarius01", "herholdt@aquarius01: Permission denied (publickey,password).\n", 255)
    assert no_key.needs_ssh_key
    assert no_key.error == "no access yet: you need to log in to this node once first (mqnodes --switch 1)"
    assert "(mqnodes --switch 4)" in mq.explain_ssh_failure("aquarius04", "Permission denied", 255).error
    assert "host key changed" in mq.explain_ssh_failure("a", "Host key verification failed.\n", 255).error
    assert mq.explain_ssh_failure("a", "ssh: Could not resolve hostname a: x\n", 255).error == "unknown host name"
    other = mq.explain_ssh_failure("a", "", 255)
    assert other.error == "unreachable (ssh exit code 255)" and not other.needs_ssh_key


def test_key_help_creates_a_key_only_when_there_is_none(tmp_path):
    help_without_key = "\n".join(mq.ssh_key_help(str(tmp_path)))
    assert "ssh-keygen -t ed25519" in help_without_key
    assert "cat ~/.ssh/id_ed25519.pub >> ~/.ssh/authorized_keys" in help_without_key

    (tmp_path / "id_rsa.pub").write_text("ssh-rsa AAAA test")
    help_with_key = "\n".join(mq.ssh_key_help(str(tmp_path)))
    assert "ssh-keygen" not in help_with_key
    assert "cat ~/.ssh/id_rsa.pub >> ~/.ssh/authorized_keys" in help_with_key


def test_nodes_without_a_key_still_show_the_rest():
    reports = [mq.NodeReport("aquarius01", error="no access yet: you need to log in to this node once first",
                             needs_ssh_key=True),
               mq.NodeReport("aquarius04", load1=1.0, ncpu=40, mem_total_kb=100, mem_avail_kb=50,
                             sessions=(zellij("here"),), panes=(("zellij", "here", "/x"),))]
    nodes = "\n".join(mq.render_nodes(reports, "aquarius04"))
    assert "-> aquarius04" in nodes and "authorized_keys" in nodes
    assert "no access yet" in nodes and "mqnodes --switch 2" in nodes
    sessions = "\n".join(mq.render_sessions(reports, "aquarius04"))
    assert "Not checked (see LOGIN NODES): aquarius01" in sessions
    assert "aquarius04  zellij here" in sessions


# ---------------------------------------------------------------- sessions from another zellij version

def test_session_made_by_another_zellij_version_gets_its_own_attach_command():
    text = ("session\tzellij\tnew\t10\t0\tzellij\n"
            "session\tzellij\told\t10\t0\t/home/someone/bin/zellij-0.43.1\n"
            "session\ttmux\tt\t10\t0\ttmux")
    r = mq.parse_node_output("aquarius04", text)
    assert [s.attach_with for s in r.sessions] == ["zellij", "/home/someone/bin/zellij-0.43.1", "tmux"]
    out = "\n".join(mq.render_sessions([r], "elsewhere"))
    assert "old was started by a different zellij version" in out
    assert "ssh -t aquarius04.qut.edu.au /home/someone/bin/zellij-0.43.1 attach old" in out
    assert "new was started" not in out and "t was started" not in out


def test_old_style_session_lines_without_a_program_still_parse():
    r = mq.parse_node_output("aquarius01", "session\tzellij\tx\t10\t0")
    assert r.sessions == (mq.Session("zellij", "x", 10, 0, "zellij"),)


# ---------------------------------------------------------------- --switch

@pytest.mark.parametrize("text, node", [
    ("1", "aquarius01"), ("04", "aquarius04"), ("aqua3", "aquarius03"), ("AQUA02", "aquarius02"),
    ("aquarius03", "aquarius03"), ("aquarius01.qut.edu.au", "aquarius01")])
def test_node_choice_accepts_the_usual_spellings(text, node):
    assert mq.parse_node_choice(text) == node


@pytest.mark.parametrize("text", ["0", "5", "aqua", "aquarius05", "cpu1n001", "", "13"])
def test_node_choice_rejects_anything_that_is_not_a_login_node(text):
    with pytest.raises(mq.argparse.ArgumentTypeError):
        mq.parse_node_choice(text)


def test_switch_command_is_interactive_and_keeps_the_folder():
    cmd = mq.switch_command("aquarius03", "/work/my project/it's")
    assert cmd[:2] == ["ssh", "-t"] and "BatchMode=yes" not in cmd  # ssh may ask for a password
    assert cmd[-2] == "aquarius03.qut.edu.au"
    assert cmd[-1].startswith("cd '/work/my project/it'\"'\"'s' 2>/dev/null || cd;")  # quoted safely
    assert cmd[-1].endswith('exec "$SHELL" -l')


def test_switch_hands_the_terminal_over_to_ssh(monkeypatch, tmp_path, capsys):
    calls = []
    monkeypatch.setattr(mq.os, "execvp", lambda prog, argv: calls.append((prog, argv)))
    monkeypatch.setattr(mq.socket, "gethostname", lambda: "cpu1n001.hpc")
    monkeypatch.chdir(tmp_path)
    mq.main(["--switch", "2"])
    prog, argv = calls[0]
    assert prog == "ssh" and argv[-2] == "aquarius02.qut.edu.au" and str(tmp_path) in argv[-1]
    assert "Logging in to aquarius02" in capsys.readouterr().out


def test_switch_to_the_node_you_are_on_does_nothing(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(mq.os, "execvp", lambda prog, argv: calls.append(prog))
    monkeypatch.setattr(mq.socket, "gethostname", lambda: "aquarius03.qut.edu.au")
    assert mq.main(["--switch", "aqua3"]) == 0
    assert calls == [] and "already on aquarius03" in capsys.readouterr().out


# ---------------------------------------------------------------- things that could break for other users

@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")
def test_a_frozen_tmux_server_does_not_stop_the_node_scan(tmp_path):
    """SIGSTOP a real tmux server: its clients would wait forever without the time limit."""
    env = {**os.environ, "TMUX_TMPDIR": str(tmp_path)}
    env.pop("TMUX", None)
    subprocess.run(["tmux", "new-session", "-d", "-s", "frozen"], env=env, check=True)
    pid = int(subprocess.run(["tmux", "display-message", "-p", "-t", "frozen", "#{pid}"],
                             env=env, capture_output=True, text=True, check=True).stdout)
    os.kill(pid, 19)  # SIGSTOP
    try:
        started = time.time()
        out = subprocess.run(["bash", "-s"], input=mq.NODE_SCRIPT, text=True, env=env,
                             capture_output=True, timeout=60).stdout.splitlines()
        took = time.time() - started
    finally:
        os.kill(pid, 18)  # SIGCONT, so it can be shut down
        subprocess.run(["tmux", "kill-server"], env=env)
    assert took < 20, f"scan took {took:.0f}s"
    assert any(l.startswith("load\t") for l in out)  # the rest of the node still reported
    assert any(l.startswith("warning\ttmux server") and "did not answer" in l for l in out)


def test_a_node_that_answers_only_partly_still_shows_what_arrived(monkeypatch):
    partial = "load\t2.0\t40\nmem\t100\t50\nusers\t3\nsession\ttmux\tt\t10\t0\ttmux\n"

    def slow(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 20, output=partial.encode())

    monkeypatch.setattr(mq.subprocess, "run", slow)
    report = mq.scan_node("aquarius02", "elsewhere")
    assert report.error is None and report.users == 3 and [s.name for s in report.sessions] == ["t"]
    assert "only partly answered" in report.warning


def test_a_node_that_says_nothing_is_reported_as_timed_out(monkeypatch):
    def slow(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 20, output=None)

    monkeypatch.setattr(mq.subprocess, "run", slow)
    assert "timed out" in mq.scan_node("aquarius02", "elsewhere").error


def test_scan_ssh_never_asks_for_a_terminal_or_password(monkeypatch):
    seen = {}

    def fake(cmd, **kwargs):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="load\t1.0\t4\n", stderr="")

    monkeypatch.setattr(mq.subprocess, "run", fake)
    mq.scan_node("aquarius02", "elsewhere")
    cmd = seen["cmd"]
    assert "-T" in cmd and "BatchMode=yes" in cmd and "ClearAllForwardings=yes" in cmd


def test_several_warnings_from_one_node_are_all_kept():
    r = mq.parse_node_output("aquarius01", "warning\ttmux server \"x\" did not answer\nwarning\tzellij not found")
    assert r.warning == 'tmux server "x" did not answer; zellij not found'


def test_a_huge_snakemake_log_is_read_without_loading_it_whole(tmp_path):
    log_dir = tmp_path / ".snakemake" / "log"
    log_dir.mkdir(parents=True)
    with open(log_dir / "big.snakemake.log", "w") as f:
        f.write("Submitted job 1 with external jobid '1.aqua'.\n")
        for _ in range(60000):
            f.write("[Thu Oct 22 09:19:50 2026] rule noise: some long progress line " + "y" * 60 + "\n")
        f.write("Submitted job 2 with external jobid '2.aqua'.\n")
    clear_caches()
    assert mq.snakemake_submitted_jobs(str(tmp_path), 0.0) == {"1.aqua", "2.aqua"}


def test_a_big_queue_is_fetched_in_batches_and_put_back_together(monkeypatch):
    import json
    ids = [f"{n}.aqua" for n in range(1, 1201)]  # three batches of up to 500

    def fake(cmd, **kwargs):
        if cmd[0] == "qselect":
            return subprocess.CompletedProcess(cmd, 0, stdout="\n".join(ids), stderr="")
        jobs = {i: {"job_state": "Q", "Submit_Host": "aquarius01.x"} for i in cmd[4:]}
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"Jobs": jobs}), stderr="")

    monkeypatch.setattr(mq.subprocess, "run", fake)
    report = mq.fetch_jobs("someone")
    assert report.error is None and len(report.jobs) == 1200
    assert [j.job_id for j in report.jobs] == sorted(ids)


def test_a_failed_batch_is_reported_but_the_others_are_kept(monkeypatch):
    import json
    ids = [f"{n}.aqua" for n in range(1, 1001)]

    def fake(cmd, **kwargs):
        if cmd[0] == "qselect":
            return subprocess.CompletedProcess(cmd, 0, stdout="\n".join(ids), stderr="")
        if cmd[4] == "501.aqua":
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="server busy")
        jobs = {i: {"job_state": "R", "Submit_Host": "aquarius01"} for i in cmd[4:]}
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"Jobs": jobs}), stderr="")

    monkeypatch.setattr(mq.subprocess, "run", fake)
    report = mq.fetch_jobs("someone")
    assert len(report.jobs) == 500 and "server busy" in report.error


def test_no_jobs_at_all_is_not_an_error(monkeypatch):
    monkeypatch.setattr(mq.subprocess, "run",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="", stderr=""))
    assert mq.fetch_jobs("someone") == mq.JobReport()


def test_switch_from_a_deleted_folder_lands_in_home(monkeypatch):
    calls = []
    monkeypatch.setattr(mq.os, "execvp", lambda prog, argv: calls.append(argv))
    monkeypatch.setattr(mq.socket, "gethostname", lambda: "cpu1n001")

    def gone():
        raise FileNotFoundError("deleted")

    monkeypatch.setattr(mq.os, "getcwd", gone)
    mq.main(["--switch", "1"])
    assert os.path.expanduser("~") in calls[0][-1]
