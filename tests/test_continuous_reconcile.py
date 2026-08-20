"""Continuous Mode worker integration: desired_state reconcile (THE reboot
test), the continuous lane, stop semantics, and the crash-relaunch loop."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import threading
import time
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
FAKEBIN = TESTS / "fakebin"
PW = "cont-secret-DO-NOT-LEAK-77"

CONT_SPEC = """run:
  label: cont-test
  edition: advanced
  tshirt_size: 4c16g
target:
  host: db.example.invalid
  port: 5432
  database: sbtest
  user: doadmin
  password_env: PGB_TARGET_PASSWORD
  sslmode: require
workload:
  type: oltp_read_write
  tables: 4
  table_size: 1000
continuous:
  threads: 2
  segment_time_s: 1
  segment_kill_grace_s: 2
"""


@pytest.fixture()
def wcfg(tmp_path, monkeypatch):
    """Worker-level fixture: fakebin on PATH, harness bin resolved, fresh DB."""
    for exe in ("sysbench", "psql"):
        p = FAKEBIN / exe
        p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    import sys
    venv_bin = Path(sys.executable).parent
    monkeypatch.setenv("PATH", f"{FAKEBIN}{os.pathsep}{venv_bin}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("PGBENCH_HARNESS_BIN", str(venv_bin / "pgbench-harness"))
    monkeypatch.setenv("PGB_PROBE_GRACE_S", "0.3")
    monkeypatch.setenv("FAKE_SYSBENCH_REALTIME", "1")
    state = tmp_path / "fakestate"
    state.mkdir()
    monkeypatch.setenv("FAKE_PSQL_STATE", str(state))
    data = tmp_path / "data"
    monkeypatch.setenv("PGBENCH_DATA_DIR", str(data))
    monkeypatch.setenv("PGBENCH_DB", str(data / "pgbench.db"))
    from pgbench_webapp.config import ensure_dirs, load_config
    from pgbench_webapp.db import migrate
    cfg = load_config()
    ensure_dirs(cfg)
    migrate(cfg.db_path)
    return cfg


def _conn(cfg):
    from pgbench_webapp.db import connect
    return connect(cfg.db_path)


def _mk_cont_run_dir(cfg, run_id: str, status: str = "running") -> Path:
    from pgbench_harness.manifest import Manifest
    run_dir = cfg.results_dir / run_id
    (run_dir / "raw").mkdir(parents=True)
    m = Manifest(run_id=run_id, label="cont-test", edition="advanced",
                 tshirt_size="4c16g", mode="continuous")
    m.status = status
    m.save(run_dir)
    return run_dir


def _enqueue_cont(conn, target_id=None, desired="running", run_id=None,
                  state=None, pid=None) -> int:
    from pgbench_webapp import queries
    jid = queries.enqueue_job(conn, "continuous", CONT_SPEC, target_id, "op",
                              desired_state=desired)
    fields = {}
    if run_id is not None:
        fields["run_id"] = run_id
    if state is not None:
        fields["state"] = state
    if pid is not None:
        fields["pid"] = pid
    if fields:
        queries.update_job(conn, jid, **fields)
    return jid


# ── THE reboot test ────────────────────────────────────────────────

def test_reboot_dead_pid_desired_running_requeues(wcfg):
    """Droplet reboot: job 'running', pid dead, desired_state='running' ->
    re-queued (NOT failed), loadgen_restart event appended to the run's
    events.jsonl, and an informational harness_relaunch alert stored."""
    from pgbench_webapp import queries, worker
    cfg = wcfg
    conn = _conn(cfg)
    run_dir = _mk_cont_run_dir(cfg, "cont-run-a")
    jid = _enqueue_cont(conn, run_id="cont-run-a", state="running", pid=99999999)

    worker.reconcile_startup(cfg, conn)

    job = queries.get_job(conn, jid)
    assert job["state"] == "queued"                 # relaunch, not failed
    assert job["pid"] is None
    assert job["desired_state"] == "running"
    assert job["run_id"] == "cont-run-a"            # same run dir — new segment
    events = [json.loads(ln) for ln in
              (run_dir / "events.jsonl").read_text().splitlines()]
    assert any(e["type"] == "loadgen_restart" for e in events)
    row = conn.execute("SELECT * FROM alerts WHERE job_id=?", (jid,)).fetchone()
    assert row is not None
    assert row["type"] == "harness_relaunch" and row["severity"] == "info"
    assert row["resolved_utc"]                      # point event, pre-resolved
    conn.close()


def test_reboot_desired_stopped_converges_canceled(wcfg):
    from pgbench_webapp import queries, worker
    cfg = wcfg
    conn = _conn(cfg)
    _mk_cont_run_dir(cfg, "cont-run-b", status="stopped")
    jid = _enqueue_cont(conn, desired="stopped", run_id="cont-run-b",
                        state="running", pid=99999998)
    worker.reconcile_startup(cfg, conn)
    job = queries.get_job(conn, jid)
    assert job["state"] == "canceled"
    assert conn.execute("SELECT count(*) FROM alerts").fetchone()[0] == 0
    conn.close()


def test_alive_pid_reattaches_not_requeued(wcfg):
    """A continuous harness that SURVIVED the worker restart is re-attached
    exactly like every other kind — never killed, never re-queued."""
    from pgbench_webapp import queries, worker
    cfg = wcfg
    conn = _conn(cfg)
    child = subprocess.Popen(["sleep", "5"])
    try:
        jid = _enqueue_cont(conn, run_id=None, state="running", pid=child.pid)
        worker.reconcile_startup(cfg, conn)
        assert queries.get_job(conn, jid)["state"] == "running"   # adopted
        # regression guard: the sweep/soak dead-pid path is unchanged
        dead = queries.enqueue_job(conn, "soak", "spec: {}", None, "t")
        queries.update_job(conn, dead, state="running", pid=99999997)
        worker.reconcile_startup(cfg, conn)
        assert queries.get_job(conn, dead)["state"] == "failed"
    finally:
        child.terminate()
        conn.close()


def test_reattach_stopped_manifest_converges_canceled(wcfg):
    """A continuous run gracefully finalized to 'stopped' while the worker was
    away converges the job to canceled, never failed."""
    from pgbench_webapp import queries, worker
    cfg = wcfg
    conn = _conn(cfg)
    _mk_cont_run_dir(cfg, "cont-run-c", status="stopped")
    jid = _enqueue_cont(conn, desired="stopped", run_id="cont-run-c",
                        state="running")
    child = subprocess.Popen(["sleep", "0.5"])
    t = threading.Thread(target=worker._reattach_orphan,
                         args=(cfg, jid, child.pid), kwargs={"poll_s": 0.2})
    t.start()
    child.wait()
    t.join(timeout=30)
    assert queries.get_job(conn, jid)["state"] == "canceled"
    conn.close()


def test_reconcile_run_dir_deleted_from_disk(wcfg):
    """Reboot reconcile with the run dir GONE: still re-queued (the worker
    starts a fresh run dir on relaunch); the event append degrades silently."""
    from pgbench_webapp import queries, worker
    cfg = wcfg
    conn = _conn(cfg)
    jid = _enqueue_cont(conn, run_id="cont-run-vanished", state="running",
                        pid=99999996)
    worker.reconcile_startup(cfg, conn)
    assert queries.get_job(conn, jid)["state"] == "queued"
    conn.close()


# ── stop semantics / desired-state wins ────────────────────────────

def test_stop_sets_desired_state_before_anything(wcfg):
    from pgbench_webapp import contworker, queries, worker
    cfg = wcfg
    conn = _conn(cfg)
    jid = _enqueue_cont(conn)                        # queued
    assert worker.stop_job_process(cfg, conn, jid) is True
    job = queries.get_job(conn, jid)
    assert job["desired_state"] == "stopped"
    assert job["state"] == "canceled"
    # housekeeping must NOT resurrect an explicitly stopped workload
    contworker.housekeeping(cfg, conn)
    assert queries.get_job(conn, jid)["state"] == "canceled"
    conn.close()


def test_housekeeping_requeues_crashed_harness(wcfg):
    """A continuous job that ended while desired_state='running' is relaunched
    by the housekeeping tick — after the holdoff, exactly once."""
    from pgbench_webapp import contworker, queries
    cfg = wcfg
    conn = _conn(cfg)
    jid = _enqueue_cont(conn, run_id=None, state="failed")
    # fresh crash (finished just now): the holdoff defers the relaunch
    queries.update_job(conn, jid, finished_utc="2999-01-01T00:00:00Z")
    contworker.housekeeping(cfg, conn)
    assert queries.get_job(conn, jid)["state"] == "failed"
    # old crash: relaunched
    queries.update_job(conn, jid, finished_utc="2020-01-01T00:00:00Z")
    contworker.housekeeping(cfg, conn)
    assert queries.get_job(conn, jid)["state"] == "queued"
    # idempotent — a queued job is not touched again
    contworker.housekeeping(cfg, conn)
    assert queries.get_job(conn, jid)["state"] == "queued"
    conn.close()


# ── the continuous lane ─────────────────────────────────────────────

def test_claim_lanes_are_independent(wcfg):
    from pgbench_webapp import queries
    cfg = wcfg
    conn = _conn(cfg)
    # bench lane full (max_concurrency=1) — a queued continuous still claims
    b = queries.enqueue_job(conn, "run", "spec: {}", None, "t")
    queries.update_job(conn, b, state="running")
    cont_id = _enqueue_cont(conn)
    claimed = queries.claim_next_job(conn, 1, continuous_cap=4)
    assert claimed is not None and claimed["id"] == cont_id

    # continuous lane full — a queued continuous is NOT claimable, but a
    # queued benchmark is (once the bench lane has room)
    for _ in range(3):
        j = _enqueue_cont(conn)
        queries.update_job(conn, j, state="running")
    assert queries.continuous_running_count(conn) == 4
    blocked = _enqueue_cont(conn)
    queries.update_job(conn, b, state="done")        # free the bench lane
    r = queries.enqueue_job(conn, "run", "spec: {}", None, "t")
    claimed = queries.claim_next_job(conn, 1, continuous_cap=4)
    assert claimed is not None and claimed["id"] == r    # skipped the blocked cont
    assert queries.get_job(conn, blocked)["state"] == "queued"
    # both lanes full -> nothing claimable
    assert queries.claim_next_job(conn, 1, continuous_cap=4) is None
    conn.close()


def test_running_count_excludes_indefinite_lanes(wcfg):
    from pgbench_webapp import queries
    conn = _conn(wcfg)
    j1 = _enqueue_cont(conn, state="running")
    m = queries.enqueue_job(conn, "ops_monitor", "spec: {}", None, "t")
    queries.update_job(conn, m, state="running")
    assert queries.running_count(conn) == 0
    assert queries.continuous_running_count(conn) == 1
    conn.close()


# ── worker-level end to end: start -> stop -> reboot -> relaunch ───

def _wait(predicate, timeout_s: float = 30.0, every: float = 0.2):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        v = predicate()
        if v:
            return v
        time.sleep(every)
    raise AssertionError("condition not met in time")


def test_worker_e2e_stop_then_reboot_relaunch(wcfg):
    """The full circle at worker level: run_job drives the real harness CLI
    (fakebin sysbench), a stop converges to canceled + manifest 'stopped';
    a simulated reboot relaunches into the SAME run dir with a NEW segment."""
    from pgbench_webapp import queries, worker
    from pgbench_webapp.secrets_store import SecretStore
    cfg = wcfg
    conn = _conn(cfg)
    store = SecretStore(cfg.secret_key_path, cfg.data_dir / "secrets.enc")
    store.set("target:t1:password", PW)
    tid = queries.create_target(conn, "t1", "db.example.invalid", 5432,
                                "sbtest", "doadmin", "require",
                                "target:t1:password")
    jid = _enqueue_cont(conn, target_id=tid)
    claimed = queries.claim_next_job(conn, 1, continuous_cap=4)
    assert claimed is not None and claimed["id"] == jid
    t = threading.Thread(target=worker._run_job_threaded, args=(cfg, store, jid),
                         daemon=True)
    t.start()
    # the harness links its run dir within the first log lines
    job = _wait(lambda: (lambda j: j if j and j["run_id"] and j["pid"] else None)(
        queries.get_job(conn, jid)))
    run_id = job["run_id"]
    run_dir = cfg.results_dir / run_id
    _wait(lambda: list(run_dir.glob("raw/cont_seg*.log")))
    # user stop: desired_state wins forever after
    assert worker.stop_job_process(cfg, conn, jid) is True
    t.join(timeout=30)
    assert not t.is_alive()
    job = queries.get_job(conn, jid)
    assert job["state"] == "canceled" and job["desired_state"] == "stopped"
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["status"] == "stopped"
    segs_before = len(list(run_dir.glob("raw/cont_seg*.log")))
    assert segs_before >= 1

    # ── simulated reboot: user resumes (desired running), then the pid is
    # dead at startup — reconcile relaunches into the same run dir.
    queries.update_job(conn, jid, desired_state="running", state="running",
                       pid=99999995, pid_start="")
    worker.reconcile_startup(cfg, conn)
    assert queries.get_job(conn, jid)["state"] == "queued"
    claimed = queries.claim_next_job(conn, 1, continuous_cap=4)
    assert claimed is not None and claimed["id"] == jid
    t = threading.Thread(target=worker._run_job_threaded, args=(cfg, store, jid),
                         daemon=True)
    t.start()
    _wait(lambda: len(list(run_dir.glob("raw/cont_seg*.log"))) > segs_before)
    job = queries.get_job(conn, jid)
    assert job["run_id"] == run_id                   # SAME run — one timeline
    assert worker.stop_job_process(cfg, conn, jid) is True
    t.join(timeout=30)
    assert not t.is_alive()
    assert json.loads((run_dir / "manifest.json").read_text())["status"] == "stopped"
    # no secret in any artifact, even across relaunches
    leaks = [p for p in run_dir.rglob("*")
             if p.is_file() and PW.encode() in p.read_bytes()]
    assert not leaks
    conn.close()
