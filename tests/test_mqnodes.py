import importlib.machinery
import json
import os
import importlib.util
import subprocess
import sys
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
mq.Colors.disable()  # ANSI codes would skew width assertions


def run(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *args],
                          text=True, capture_output=True)


def test_parse_node_output_reads_all_record_types():
    text = "\n".join([
        "L\t3.5\t40",
        "M\t1000\t250",
        "U\t7",
        "S\tmy session\t1761000000\t2",
        "X\told-one",
        "S\tbroken\tnot-a-number\t0",
    ])
    r = mq.parse_node_output("aquarius01", text)
    assert (r.load1, r.ncpu, r.mem_total_kb, r.mem_avail_kb, r.users) == (3.5, 40, 1000, 250, 7)
    assert r.sessions == (mq.ZellijSession("my session", 1761000000, 2),)
    assert r.exited == ("old-one",)
    assert r.error is None


def test_parse_jobs_json_extracts_state_submit_host_and_time():
    jobs = mq.parse_jobs_json(QSTAT_JSON.read_text())
    assert [(j.job_id, j.state, j.submit_host) for j in jobs] == [
        ("100.aqua", "R", "aquarius02"), ("101.aqua", "Q", "aquarius03"), ("102.aqua", "H", "aquarius03")]


def test_pane_label_walks_up_to_pixi_or_git(tmp_path):
    repo = tmp_path / "aviary-v0.13.14"
    (repo / "runs" / "sample1").mkdir(parents=True)
    (repo / "pixi.toml").write_text("")
    plain = tmp_path / "nothing"
    plain.mkdir()
    mq.find_repo_root.cache_clear()
    assert mq.pane_label(str(repo / "runs" / "sample1")) == "aviary-v0.13.14/runs/sample1"
    assert mq.pane_label(str(plain)) == str(plain)


def test_find_repo_root_ignores_a_home_directory_repo(tmp_path):
    home = tmp_path / "home" / "someone"
    (home / ".git").mkdir(parents=True)
    (home / "work").mkdir()
    mq.find_repo_root.cache_clear()
    assert mq.find_repo_root(str(home / "work")) == ""


def test_exited_sessions_are_unioned_and_exclude_live_ones():
    reports = [
        mq.NodeReport("aquarius01", exited=("a", "b")),
        mq.NodeReport("aquarius02", sessions=(mq.ZellijSession("b", 0, 0),), exited=("a", "c")),
    ]
    out = "\n".join(mq.render_zellij(reports, "elsewhere"))
    assert "2 resurrectable exited sessions" in out
    assert "aquarius02  b" in out


def test_unreachable_node_is_reported_not_fatal():
    out = "\n".join(mq.render_nodes([mq.NodeReport("aquarius09", error="unreachable (x)")], "x"))
    assert "aquarius09" in out and "unreachable (x)" in out


def test_summary_counts_by_submit_host():
    result = run("--no-nodes", "--qstat-json-file", str(QSTAT_JSON))
    assert result.returncode == 0, result.stderr
    assert "Jobs: 1 running, 1 queued, 1 held  (3 total)" in result.stdout
    lines = result.stdout.splitlines()
    assert any(l.split() == ["aquarius02", "1", "0", "0", "0"] for l in lines)
    assert any(l.split() == ["aquarius03", "0", "1", "1", "0"] for l in lines)


LAYOUT_KDL = """layout {
    cwd "/base"
    tab name="Tab #1" focus=true {
        pane size=1 borderless=true {
            plugin location="zellij:tab-bar"
        }
        pane split_direction="vertical" {
            pane command="pixi" cwd="repo/backend" size="50%" {
                args "shell"
            }
            pane command="-bash" cwd="/abs/elsewhere"
            pane command="tee" cwd="repo/backend"
        }
    }
    tab name="Tab #2" cwd="other" {
        pane command="snakemake" cwd="sub"
    }
}
"""


def test_parse_layout_panes_resolves_tab_and_base_cwds():
    panes = mq.parse_layout_panes(LAYOUT_KDL)
    assert panes == [
        mq.PaneDir("/base/repo/backend", "pixi"),  # duplicate dir keeps first command
        mq.PaneDir("/abs/elsewhere", ""),          # plain shells show no command
        mq.PaneDir("/base/other/sub", "snakemake"),
    ]


def test_find_repo_root_prefers_git_checkout_over_nested_pixi(tmp_path):
    checkout = tmp_path / "singlem-myversion"
    nested = checkout / "extras" / "update_metapackage"
    nested.mkdir(parents=True)
    (checkout / ".git").mkdir()
    (nested / "pixi.toml").write_text("")
    mq.find_repo_root.cache_clear()
    assert mq.find_repo_root(str(nested)) == str(checkout)
    assert mq.pane_label(str(nested)) == "singlem-myversion/extras/update_metapackage"
    assert mq.pane_label(str(checkout)) == "singlem-myversion"


def write_log(directory, name, text, mtime):
    log_dir = directory / ".snakemake" / "log"
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / name
    path.write_text(text)
    os.utime(path, (mtime, mtime))


def test_snakemake_job_ids_reads_only_logs_newer_than_since(tmp_path):
    write_log(tmp_path, "old.snakemake.log", "Submitted job 1 with external jobid '1.aqua'.", 1000)
    write_log(tmp_path, "a.snakemake.log", "Submitted job 2 with external jobid '2.aqua'.", 5000)
    write_log(tmp_path, "b.snakemake.log", "Submitted job 3 with external jobid '3.aqua'.\nnoise", 6000)
    mq.snakemake_job_ids.cache_clear()
    assert mq.snakemake_job_ids(str(tmp_path), 4000.0) == {"2.aqua", "3.aqua"}
    assert mq.snakemake_job_ids(str(tmp_path / "missing"), 0.0) == frozenset()


def test_link_jobs_and_zellij_render_show_counts_per_pane(tmp_path):
    run_dir = tmp_path / "myrepo"
    (run_dir / ".git").mkdir(parents=True)
    write_log(run_dir, "x.snakemake.log",
              "external jobid '10.aqua'\nexternal jobid '11.aqua'\nexternal jobid '99.aqua'", 5000)
    jobs = [mq.Job("10.aqua", "R", "aquarius04", 4900.0),
            mq.Job("11.aqua", "Q", "aquarius04", 4950.0),
            mq.Job("12.aqua", "Q", "aquarius04", 4990.0)]
    panes = (mq.SessionPanes("aquarius04", "cowbell", (mq.PaneDir(str(run_dir), "tee"),)),)
    mq.snakemake_job_ids.cache_clear()
    mq.find_repo_root.cache_clear()

    links = mq.link_jobs(panes, jobs)

    assert set(links) == {"10.aqua", "11.aqua"}  # 99 finished, 12 came from elsewhere
    assert links["10.aqua"] == mq.JobLink("aquarius04", "cowbell", str(run_dir))
    reports = [mq.NodeReport("aquarius04", sessions=(mq.ZellijSession("cowbell", 0, 0),))]
    out = mq.render_zellij(reports, "elsewhere", panes, links, jobs)
    header = next(l for l in out if l.startswith("Session"))
    assert header.split() == ["Session", "Pane", "directory", "Cmd", "Run", "Queue", "Held"]
    pane_line = next(l for l in out if "myrepo" in l)
    assert pane_line.split() == ["cowbell", "myrepo", "tee", "1", "1", "-"]


def test_parse_pbs_time():
    assert mq.parse_pbs_time("Wed Oct 21 15:35:43 2026") > 0
    assert mq.parse_pbs_time(None) == 0.0
    assert mq.parse_pbs_time("garbage") == 0.0
