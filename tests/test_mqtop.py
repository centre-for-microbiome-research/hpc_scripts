import subprocess
import sys
import re
import pstats
from pathlib import Path

ANSI = re.compile(r"\x1b\[[0-9;]*m")


def split_cols(line):
    line = ANSI.sub("", line.rstrip("\n"))
    parts = re.split(r"\s{2,}", line)
    return [p.strip() for p in parts]


def test_mqtop_print_first_page():
    repo = Path(__file__).resolve().parents[1]
    script = repo / "bin" / "mqtop"
    qstat_file = repo / "tests" / "data" / "qstat.json"
    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--print-first-page",
            "--qstat-json",
            str(qstat_file),
        ],
        text=True,
        capture_output=True,
        check=True,
    )
    lines = result.stdout.splitlines()
    assert len(lines) >= 5
    header = [c for c in split_cols(lines[0]) if c]
    assert header == [
        "job_id",
        "name",
        "time used",
        "progress",
        "walltime",
        "waited",
        "age",
        "CPU",
        "cpu%",
        "RAM(G)",
        "ram%",
        "state",
        "queue",
        "note",
    ]
    assert "222.server" in lines[1]
    assert "almostdone" in lines[1]
    assert "  2" in lines[1]
    assert "R      batch" in lines[1]
    assert any("123.server" in line for line in lines)


def test_mqtop_alignment_wide_chars():
    repo = Path(__file__).resolve().parents[1]
    script = repo / "bin" / "mqtop"
    import runpy, unicodedata

    def width(ch):
        if unicodedata.combining(ch):
            return 0
        return 2 if unicodedata.east_asian_width(ch) in ("F", "W") else 1

    def display_index(line, text):
        idx = line.index(text)
        return sum(width(ch) for ch in line[:idx])

    mod = runpy.run_path(str(script))
    jobs = [
        {
            "id": "1.server",
            "name": "test",
            "queue": "batch",
            "state": "R",
            "ncpus": 1,
            "mem_request_gb": 1,
            "walltime_used": 0,
            "walltime_total": 0,
        },
        {
            "id": "2.server",
            "name": "🐍test",
            "queue": "batch",
            "state": "R",
            "ncpus": 1,
            "mem_request_gb": 1,
            "walltime_used": 0,
            "walltime_total": 0,
        },
    ]
    lines, _ = mod["format_jobs"](jobs, 200)
    header, row_plain, row_emoji = [ANSI.sub("", l) for l in lines[:3]]
    q_idx = display_index(header, "queue")
    assert display_index(row_plain, "batch") == q_idx
    assert display_index(row_emoji, "batch") == q_idx


def test_mqtop_history_only_print_first_page():
    repo = Path(__file__).resolve().parents[1]
    script = repo / "bin" / "mqtop"
    qstat_f = repo / "tests" / "data" / "qstat_empty.json"
    qstat_xf = repo / "tests" / "data" / "qstatx_finished.json"
    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--print-first-page",
            "--qstat-json",
            str(qstat_f),
            "--qstatx-json",
            str(qstat_xf),
        ],
        text=True,
        capture_output=True,
        check=True,
    )
    lines = [l for l in result.stdout.splitlines() if l]
    assert len(lines) >= 1
    header = [c for c in split_cols(lines[0]) if c]
    assert header == [
        "job_id",
        "name",
        "time used",
        "progress",
        "walltime",
        "waited",
        "age",
        "CPU",
        "cpu%",
        "RAM(G)",
        "ram%",
        "state",
        "queue",
        "note",
    ]
    assert any("10592488" in line for line in lines[1:])
    assert any("🐍semibin.23.sh" in line for line in lines[1:])
    assert lines.count(lines[0]) == 1


def test_mqtop_profile(tmp_path):
    repo = Path(__file__).resolve().parents[1]
    script = repo / "bin" / "mqtop"
    qstat_file = repo / "tests" / "data" / "qstat.json"
    prof = tmp_path / "stats.prof"
    subprocess.run(
        [
            sys.executable,
            str(script),
            "--print-first-page",
            "--qstat-json",
            str(qstat_file),
            "--profile",
            str(prof),
        ],
        text=True,
        capture_output=True,
        check=True,
    )
    assert prof.exists()
    stats = pstats.Stats(str(prof))
    assert any("get_jobs" in func for (_, _, func) in stats.stats)
