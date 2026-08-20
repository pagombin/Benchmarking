"""Bug-bash regression tests (see docs/BUGBASH.md for the ledger). Each test
names its bug id."""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml


@pytest.fixture()
def bcfg(tmp_path, monkeypatch):
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


# ── B-016: two workers against one data dir ─────────────────────────

def test_worker_singleton_lock(bcfg):
    from pgbench_webapp import worker
    lock1 = worker._acquire_singleton_lock(bcfg)
    assert lock1 is not None
    assert worker._acquire_singleton_lock(bcfg) is None      # second holder refused
    with pytest.raises(SystemExit, match="already holds the data-dir lock"):
        worker.worker_loop(bcfg)
    lock1.close()                                            # released -> acquirable again
    lock2 = worker._acquire_singleton_lock(bcfg)
    assert lock2 is not None
    lock2.close()


# ── B-017: a stop during the relaunch backoff must land promptly ────

def test_supervisor_stop_during_backoff_is_prompt(bcfg, tmp_path, monkeypatch):
    import logging
    from pgbench_harness import continuous as cont
    from pgbench_harness.manifest import Manifest
    from pgbench_harness.spec import parse_spec
    from conftest import FAKEBIN, TEST_PASSWORD, make_spec_doc
    import os
    import stat
    for exe in ("sysbench", "psql"):
        p = FAKEBIN / exe
        p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", f"{FAKEBIN}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("PGB_TARGET_PASSWORD", TEST_PASSWORD)
    monkeypatch.setenv("FAKE_SYSBENCH_EXIT_NETWORK", "1")
    monkeypatch.setattr(cont, "_backoff_s", lambda *_a, **_k: 60.0)  # a LONG sleep

    doc = make_spec_doc()
    doc.pop("sweep")
    doc.pop("report", None)
    doc["continuous"] = {"threads": 2}
    spec = parse_spec(doc)
    run_dir = tmp_path / "run"
    (run_dir / "raw").mkdir(parents=True)
    manifest = Manifest(run_id="r", label="t", edition="advanced",
                        tshirt_size="s", mode="continuous")
    manifest.save(run_dir)
    stop: dict = {"flag": False, "procs": {}}
    t = threading.Timer(1.5, lambda: stop.update(flag=True))
    t.start()
    t0 = time.monotonic()
    status = cont._continuous_supervisor(spec, TEST_PASSWORD, run_dir, manifest,
                                         logging.getLogger("t"), stop)
    elapsed = time.monotonic() - t0
    assert status == "stopped"
    assert elapsed < 10, f"stop took {elapsed:.1f}s — waited out the backoff"


# ── B-019: SQLITE_BUSY during ingest loses no data ──────────────────

def test_ingest_locked_db_retries_without_loss(bcfg):
    import sqlite3
    from pgbench_webapp import contmetrics, queries
    cfg = bcfg
    conn = _conn(cfg)
    conn.execute("PRAGMA busy_timeout=200")           # fail fast in the test
    jid = queries.enqueue_job(conn, "continuous", "spec: {}", None, "t",
                              desired_state="running")
    queries.update_job(conn, jid, run_id="r1", state="running")
    raw = cfg.results_dir / "r1" / "raw"
    raw.mkdir(parents=True)
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with open(raw / "cont_seg0001.log", "w") as fh:
        for i in range(30):
            ts = (base + timedelta(seconds=i)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            fh.write(f"{ts}\t[ 1s ] thds: 2 tps: 100.00 qps: 2000.00 "
                     "(r/w/o: 1000.00/600.00/400.00) lat (ms,99%): 45.00 "
                     "err/s 0.00 reconn/s: 0.00\n")

    blocker = _conn(cfg)
    blocker.execute("BEGIN EXCLUSIVE")                # hold the write lock
    try:
        with pytest.raises(sqlite3.OperationalError):
            contmetrics.ingest_job(cfg, conn, jid, "r1")
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
    # nothing half-ingested that a retry would skip: cursor did not advance
    assert contmetrics._cursor(conn, jid, "cont_seg0001") == 0
    assert contmetrics.ingest_job(cfg, conn, jid, "r1") == 30   # full recovery
    assert conn.execute("SELECT count(*) FROM cont_samples WHERE job_id=?",
                        (jid,)).fetchone()[0] == 30
    conn.close()


# ── B-020: retention deletes are batched, and complete ──────────────

def test_batched_delete_clears_large_backlog(bcfg):
    from pgbench_webapp import contmetrics, queries
    cfg = bcfg
    conn = _conn(cfg)
    jid = queries.enqueue_job(conn, "continuous", "spec: {}", None, "t")
    old = datetime.now(timezone.utc) - timedelta(hours=200)
    rows = [(jid, (old + timedelta(seconds=i)).strftime("%Y-%m-%dT%H:%M:%SZ"), 1.0)
            for i in range(12000)]
    conn.executemany("INSERT OR IGNORE INTO cont_samples(job_id, ts_utc, tps) "
                     "VALUES (?,?,?)", rows)
    out = contmetrics.prune(cfg, conn)
    assert out["samples"] == 12000
    assert conn.execute("SELECT count(*) FROM cont_samples").fetchone()[0] == 0
    conn.close()


# ── B-003 / B-004: CLI tolerance for the continuous mode ────────────

def test_compare_refuses_continuous_cleanly(tmp_path):
    from pgbench_harness.compare import compare_runs
    from pgbench_harness.errors import ReportError
    from pgbench_harness.manifest import Manifest
    dirs = []
    for i in range(2):
        d = tmp_path / f"cont-{i}"
        d.mkdir()
        Manifest(run_id=d.name, label="x", edition="advanced", tshirt_size="s",
                 mode="continuous").save(d)
        dirs.append(d)
    with pytest.raises(ReportError, match="no finite summary"):
        compare_runs(dirs, tmp_path / "out.html")


def test_run_resume_refuses_wrong_mode_dir(fake_env, tmp_path):
    import yaml as _yaml
    from pgbench_harness.cli import main
    from pgbench_harness.manifest import Manifest
    from conftest import make_spec_doc
    results = tmp_path / "results"
    bad = results / "cont-run"
    bad.mkdir(parents=True)
    Manifest(run_id="cont-run", label="x", edition="advanced", tshirt_size="s",
             mode="continuous").save(bad)
    spec = tmp_path / "sweep.yaml"
    spec.write_text(_yaml.safe_dump(make_spec_doc()), encoding="utf-8")
    rc = main(["run", "--spec", str(spec), "--results-dir", str(results),
               "--resume", "--run-dir", str(bad)])
    assert rc == 2                                   # clean refusal, not a bogus replay


# ── B-006: deleting a continuous run purges its ledger rows ─────────

def test_delete_run_purges_continuous_tables(bcfg):
    from pgbench_webapp import alerts, queries
    cfg = bcfg
    conn = _conn(cfg)
    jid = queries.enqueue_job(conn, "continuous", "spec: {}", None, "t")
    queries.update_job(conn, jid, run_id="r-del", state="canceled",
                       desired_state="stopped")
    conn.execute("INSERT INTO cont_samples(job_id, ts_utc, tps) VALUES (?,?,?)",
                 (jid, "2026-01-01T00:00:00Z", 1.0))
    conn.execute("INSERT INTO cont_rollup_1m(job_id, ts_utc, n, gap_s) "
                 "VALUES (?,?,?,?)", (jid, "2026-01-01T00:00:00Z", 60, 0))
    conn.execute("INSERT INTO cont_cursors(job_id, seg, pos) VALUES (?,?,?)",
                 (jid, "cont_seg0001", 100))
    conn.execute("INSERT INTO outages(job_id, kind, started_utc) VALUES (?,?,?)",
                 (jid, "read", "2026-01-01T00:00:00Z"))
    alerts.store_alert(conn, type="latency", severity="warn",
                       dedup_key=f"job:{jid}:l", job_id=jid)
    queries.purge_continuous_data(conn, jid)
    for table in ("cont_samples", "cont_rollup_1m", "cont_cursors",
                  "outages", "alerts"):
        assert conn.execute(f"SELECT count(*) FROM {table} WHERE job_id=?",
                            (jid,)).fetchone()[0] == 0, table
    conn.close()


# ── B-007: expired sessions are pruned on login ─────────────────────

def test_expired_sessions_pruned_on_login(bcfg):
    from fastapi.testclient import TestClient
    from pgbench_webapp import admin, queries
    from pgbench_webapp.app import create_app
    cfg = bcfg
    admin.create_admin("admin", "apw")
    conn = _conn(cfg)
    queries.create_session(conn, "stale-token", 1, "2020-01-01T00:00:00Z")
    assert conn.execute("SELECT count(*) FROM sessions").fetchone()[0] == 1
    client = TestClient(create_app(cfg))
    r = client.post("/login", data={"username": "admin", "password": "apw"},
                    follow_redirects=False)
    assert r.status_code == 303
    rows = list(conn.execute("SELECT token FROM sessions"))
    assert len(rows) == 1 and rows[0]["token"] != "stale-token"
    conn.close()
