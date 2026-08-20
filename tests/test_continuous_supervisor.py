"""Continuous-mode supervisor: relaunch/backoff/classification, segment
numbering, planned rotation, SIGTERM finalize, state.json heartbeat."""

from __future__ import annotations

import json
import logging
import os
import random
import signal
import threading
import time
from pathlib import Path

import pytest
import yaml

from pgbench_harness import continuous as cont
from pgbench_harness.cli import main
from pgbench_harness.errors import RunError
from pgbench_harness.manifest import Manifest
from pgbench_harness.spec import parse_spec

from conftest import TEST_PASSWORD, make_spec_doc


def cont_doc(**cont_over):
    doc = make_spec_doc()
    doc.pop("sweep")
    doc.pop("report", None)
    doc["continuous"] = {"threads": 2, **cont_over}
    return doc


# ── pure units ──────────────────────────────────────────────────────

def test_classify_error() -> None:
    assert cont.classify_error(
        'FATAL: password authentication failed for user "doadmin"') == "auth"
    assert cont.classify_error(
        "FATAL: no pg_hba.conf entry for host") == "auth"
    assert cont.classify_error(
        "could not connect to server: Connection refused") == "network"
    assert cont.classify_error("server closed the connection unexpectedly") == "network"
    assert cont.classify_error("connection timed out") == "network"
    assert cont.classify_error("PANIC: something exotic") == "other"
    # auth wins even when network words also appear
    assert cont.classify_error(
        "connection to server failed: password authentication failed") == "auth"


def test_backoff_ladder_and_jitter() -> None:
    rng = random.Random(42)
    ladder = (1, 2, 5, 10, 30, 60)
    for fails, base in [(1, 1), (2, 2), (3, 5), (6, 60), (99, 60)]:
        for _ in range(50):
            d = cont._backoff_s(fails, ladder, "network", rng)
            assert 0.1 <= d <= base
    # auth jumps straight to the cap
    for _ in range(50):
        assert cont._backoff_s(1, ladder, "auth", rng) <= 60
    seen = [cont._backoff_s(1, ladder, "auth", rng) for _ in range(200)]
    assert max(seen) > 10        # actually spread across [0, 60], not stuck low


def test_next_segment_number(tmp_path: Path) -> None:
    (tmp_path / "raw").mkdir()
    assert cont.next_segment_number(tmp_path) == 1
    (tmp_path / "raw" / "cont_seg0003.log").write_text("x")
    (tmp_path / "raw" / "cont_seg0001.log").write_text("x")
    assert cont.next_segment_number(tmp_path) == 4


def test_read_state_tolerates_missing_and_garbage(tmp_path: Path) -> None:
    assert cont.read_state(tmp_path) == {}
    (tmp_path / "state.json").write_text("{not json")
    assert cont.read_state(tmp_path) == {}


# ── supervisor (direct, stop-dict driven) ───────────────────────────

def _run_supervisor(spec_doc: dict, run_dir: Path, stop_after_s: float,
                    prev_continuous: dict | None = None) -> Manifest:
    spec = parse_spec(spec_doc)
    (run_dir / "raw").mkdir(parents=True, exist_ok=True)
    manifest = Manifest(run_id=run_dir.name, label="t", edition="advanced",
                        tshirt_size="4c16g", mode="continuous")
    if prev_continuous:
        manifest.continuous = prev_continuous
    manifest.status = "running"
    manifest.save(run_dir)
    stop: dict = {"flag": False, "procs": {}}

    def _stopper() -> None:
        stop["flag"] = True
        p = stop["procs"].get("proc")
        if p is not None:
            try:
                p.terminate()
            except OSError:
                pass

    t = threading.Timer(stop_after_s, _stopper)
    t.daemon = True
    t.start()
    logger = logging.getLogger("cont-test")
    logger.setLevel(logging.INFO)
    try:
        status = cont._continuous_supervisor(spec, TEST_PASSWORD, run_dir,
                                             manifest, logger, stop)
    finally:
        t.cancel()
    assert status == "stopped"
    manifest.save(run_dir)
    return manifest


def test_planned_rotation_is_not_a_relaunch(fake_env, tmp_path, monkeypatch) -> None:
    """1s segments rotating cleanly: multiple segments, ZERO relaunches, no
    loadgen_restart events, live CSV populated, state.json heartbeat present."""
    monkeypatch.setenv("FAKE_SYSBENCH_REALTIME", "1")
    run_dir = tmp_path / "cont-run"
    m = _run_supervisor(cont_doc(segment_time_s=1, segment_kill_grace_s=2),
                        run_dir, stop_after_s=3.4)
    doc = m.continuous
    assert doc["segments_total"] >= 2
    assert doc["relaunches"] == 0
    assert all(s["planned"] for s in doc["segments"][:-1])   # last may be cut by the stop
    assert not (run_dir / "events.jsonl").exists()           # no restart events
    logs = sorted((run_dir / "raw").glob("cont_seg*.log"))
    assert len(logs) >= 2 and logs[0].name == "cont_seg0001.log"
    csv_lines = (run_dir / "parsed" / "cont_timeseries.csv").read_text().splitlines()
    assert csv_lines[0].split(",") == cont.CONT_TIMESERIES_COLUMNS
    assert len(csv_lines) >= 3                                # >= 2 samples
    st = cont.read_state(run_dir)
    assert st["status"] == "stopped" and st["relaunches"] == 0
    assert st["last_sample_utc"]
    # secret never reaches any artifact
    leaks = [p for p in run_dir.rglob("*")
             if p.is_file() and TEST_PASSWORD.encode() in p.read_bytes()]
    assert not leaks


def test_auth_failure_backs_off_to_max_and_gives_up(fake_env, tmp_path,
                                                    monkeypatch) -> None:
    """Auth errors: classified, jump to the max backoff, and with
    max_consecutive_failures>0 the LOAD stops while the supervisor stays alive."""
    monkeypatch.setenv("FAKE_SYSBENCH_EXIT_AUTH", "1")
    run_dir = tmp_path / "cont-auth"
    m = _run_supervisor(cont_doc(restart_backoff_s=[1],
                                 max_consecutive_failures=2),
                        run_dir, stop_after_s=4.0)
    doc = m.continuous
    assert doc["relaunches"] == 2                      # gave up after exactly N
    assert doc["segments_total"] == 2
    assert all(s["error_class"] == "auth" for s in doc["segments"])
    st = cont.read_state(run_dir)
    assert st["load_stopped"] is True
    assert st["last_error_class"] == "auth"
    assert "password authentication" in st["last_error"]
    ev = [json.loads(ln) for ln in
          (run_dir / "events.jsonl").read_text().splitlines() if ln.strip()]
    assert sum(1 for e in ev if e["type"] == "loadgen_restart") == 2
    assert any(e["type"] == "note" and "gave up" in e["label"] for e in ev)


def test_network_failures_then_recovery_resets_ladder(fake_env, tmp_path,
                                                      monkeypatch) -> None:
    """Two connection-refused exits climb the ladder; the third launch streams
    samples (infinite mode) until the stop lands."""
    monkeypatch.setenv("FAKE_SYSBENCH_COUNT_FILE", str(tmp_path / "cnt"))
    monkeypatch.setenv("FAKE_SYSBENCH_FAIL_FIRST", "2")
    monkeypatch.setenv("FAKE_SYSBENCH_INFINITE", "1")
    run_dir = tmp_path / "cont-net"
    m = _run_supervisor(cont_doc(restart_backoff_s=[1, 1], segment_time_s=300),
                        run_dir, stop_after_s=7.0)
    doc = m.continuous
    assert doc["relaunches"] == 2
    classes = [s["error_class"] for s in doc["segments"][:2]]
    assert classes == ["network", "network"]
    assert doc["segments"][-1]["intervals"] >= 1       # the load actually flowed
    csv_lines = (run_dir / "parsed" / "cont_timeseries.csv").read_text().splitlines()
    assert len(csv_lines) >= 2


def test_supervisor_resumes_prior_counters(fake_env, tmp_path, monkeypatch) -> None:
    """A relaunched supervisor (reboot path) keeps start_utc and the cumulative
    relaunch counter, and appends segment numbers after the existing ones."""
    monkeypatch.setenv("FAKE_SYSBENCH_REALTIME", "1")
    run_dir = tmp_path / "cont-resume"
    (run_dir / "raw").mkdir(parents=True)
    (run_dir / "raw" / "cont_seg0002.log").write_text("")   # prior life's segments
    prev = {"run_id": run_dir.name, "start_utc": "2026-01-01T00:00:00.000000Z",
            "segments": [], "segments_total": 2, "relaunches": 3, "threads": 2}
    m = _run_supervisor(cont_doc(segment_time_s=1, segment_kill_grace_s=2),
                        run_dir, stop_after_s=1.6, prev_continuous=prev)
    doc = m.continuous
    assert doc["start_utc"] == "2026-01-01T00:00:00.000000Z"   # preserved
    assert doc["relaunches"] == 3                               # cumulative
    assert doc["segments"][0]["seg"] == 3                       # appended after 0002
    assert doc["segments_total"] >= 3


# ── cmd_continuous / CLI e2e ────────────────────────────────────────

def _sigterm_self_after(delay_s: float) -> threading.Timer:
    t = threading.Timer(delay_s, lambda: os.kill(os.getpid(), signal.SIGTERM))
    t.daemon = True
    t.start()
    return t


def test_continuous_e2e_sigterm_finalizes_stopped(fake_env, tmp_path,
                                                  monkeypatch) -> None:
    """Full CLI path: preflight, supervisor, SIGTERM -> manifest 'stopped',
    exit code 0 (a graceful stop is not a failure)."""
    monkeypatch.setenv("FAKE_SYSBENCH_REALTIME", "1")
    results = tmp_path / "results"
    spec_path = tmp_path / "cont.yaml"
    spec_path.write_text(yaml.safe_dump(
        cont_doc(segment_time_s=1, segment_kill_grace_s=2)), encoding="utf-8")
    timer = _sigterm_self_after(6.0)
    try:
        rc = main(["continuous", "--spec", str(spec_path),
                   "--results-dir", str(results)])
    finally:
        timer.cancel()
    assert rc == 0
    run_dir = sorted(d for d in results.iterdir()
                     if (d / "manifest.json").exists())[-1]
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["mode"] == "continuous"
    assert manifest["status"] == "stopped"
    assert manifest["finished_utc"]
    assert manifest["continuous"]["segments_total"] >= 1
    assert (run_dir / "state.json").exists()
    assert (run_dir / "spec.yaml").exists()
    # `report` refuses cleanly (continuous has no static report)
    with pytest.raises(RunError, match="no static HTML report"):
        from pgbench_harness.runner import cmd_report
        cmd_report(run_dir)
    # `list` tolerates the new mode
    assert main(["list", "--results-dir", str(results)]) == 0
    # no secret in any artifact
    leaks = [p for p in run_dir.rglob("*")
             if p.is_file() and TEST_PASSWORD.encode() in p.read_bytes()]
    assert not leaks


def test_continuous_e2e_resume_appends_to_same_run(fake_env, tmp_path,
                                                   monkeypatch) -> None:
    """--run-dir resume: new segments append to the same run directory and the
    original start_utc survives — the reboot relaunch path end to end."""
    monkeypatch.setenv("FAKE_SYSBENCH_REALTIME", "1")
    results = tmp_path / "results"
    spec_path = tmp_path / "cont.yaml"
    spec_path.write_text(yaml.safe_dump(
        cont_doc(segment_time_s=1, segment_kill_grace_s=2)), encoding="utf-8")
    timer = _sigterm_self_after(4.0)
    try:
        assert main(["continuous", "--spec", str(spec_path),
                     "--results-dir", str(results)]) == 0
    finally:
        timer.cancel()
    run_dir = sorted(d for d in results.iterdir()
                     if (d / "manifest.json").exists())[-1]
    m1 = json.loads((run_dir / "manifest.json").read_text())
    segs_before = len(list((run_dir / "raw").glob("cont_seg*.log")))
    assert segs_before >= 1

    timer = _sigterm_self_after(3.0)
    try:
        assert main(["continuous", "--spec", str(spec_path),
                     "--results-dir", str(results),
                     "--run-dir", str(run_dir)]) == 0
    finally:
        timer.cancel()
    m2 = json.loads((run_dir / "manifest.json").read_text())
    assert m2["continuous"]["start_utc"] == m1["continuous"]["start_utc"]
    assert len(list((run_dir / "raw").glob("cont_seg*.log"))) > segs_before
    assert m2["status"] == "stopped"
    # only one run dir exists — resume did NOT create a second run
    assert len([d for d in results.iterdir() if d.is_dir()]) == 1


def test_run_dir_wrong_mode_rejected(fake_env, tmp_path) -> None:
    results = tmp_path / "results"
    bad = results / "sweep-run"
    bad.mkdir(parents=True)
    Manifest(run_id="sweep-run", label="x", edition="advanced",
             tshirt_size="s", mode="sweep").save(bad)
    spec_path = tmp_path / "cont.yaml"
    spec_path.write_text(yaml.safe_dump(cont_doc()), encoding="utf-8")
    assert main(["continuous", "--spec", str(spec_path), "--results-dir",
                 str(results), "--run-dir", str(bad)]) == 2
