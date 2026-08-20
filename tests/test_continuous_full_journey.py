"""THE end-to-end continuous-mode journey (bug-bash section H).

Install-shape config → saved target → start continuous through the API (with
prepare) → real worker execution against fakebin → samples + rollups ingested
→ sysbench crash survived by the supervisor → droplet reboot survived by the
desired-state reconcile (same run dir, new segment, harness_relaunch alert) →
probe failure opens an outage and Slack delivery is recorded (mocked) → stop
→ desired_state respected: nothing resurrects the workload.
"""

from __future__ import annotations

import json
import os
import stat
import threading
import time
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
FAKEBIN = TESTS / "fakebin"
PW = "journey-secret-DO-NOT-LEAK"


def _wait(predicate, timeout_s: float = 30.0, every: float = 0.2, what: str = ""):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        v = predicate()
        if v:
            return v
        time.sleep(every)
    raise AssertionError(f"timed out waiting for {what or predicate}")


@pytest.fixture()
def journey(tmp_path, monkeypatch):
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
    monkeypatch.setenv("FAKE_PSQL_TABLES", "0")       # dataset starts MISSING
    monkeypatch.setenv("FAKE_SYSBENCH_COUNT_FILE", str(tmp_path / "runs.count"))
    monkeypatch.setenv("FAKE_SYSBENCH_FAIL_FIRST", "1")  # first sysbench run crashes
    data = tmp_path / "data"
    monkeypatch.setenv("PGBENCH_DATA_DIR", str(data))
    monkeypatch.setenv("PGBENCH_DB", str(data / "pgbench.db"))

    from fastapi.testclient import TestClient
    from pgbench_webapp import admin, queries
    from pgbench_webapp.app import create_app
    from pgbench_webapp.config import load_config
    from pgbench_webapp.db import connect
    from pgbench_webapp.security import hash_password
    cfg = load_config()
    admin.create_admin("admin", "apw")
    conn = connect(cfg.db_path)
    queries.create_user(conn, "op", hash_password("oppw"), "operator")
    conn.close()
    client = TestClient(create_app(cfg))
    return client, cfg


def test_the_full_journey(journey, monkeypatch):
    from pgbench_harness.continuous import read_state
    from pgbench_webapp import contmetrics, contprobe, contworker, queries, worker
    from pgbench_webapp.db import connect
    from pgbench_webapp.secrets_store import SecretStore
    client, cfg = journey
    conn = connect(cfg.db_path)
    store = SecretStore(cfg.secret_key_path, cfg.data_dir / "secrets.enc")

    # ── 0. console config: Slack on, fast probe thresholds ──
    slack_sent: list[str] = []
    r = client.post("/api/admin/settings", json={
        "slack": {"enabled": True}, "slack_webhook": "https://hooks.slack.example/x",
        "cont_alerts_config": {"probe_interval_s": 1, "probe_failures_to_down": 2},
        "base_url": "https://console.example:8443",
    }, auth=("admin", "apw"))
    assert r.status_code == 200

    # ── 1. saved target (the durable credential) ──
    tid = client.post("/api/targets", json={
        "name": "adv-nyc3", "host": "db.example.invalid", "port": 5432,
        "dbname": "sbtest", "dbuser": "doadmin", "password": PW},
        auth=("op", "oppw")).json()["id"]

    # ── 2. start the workload through the API, with prepare ──
    r = client.post("/api/continuous", json={
        "target_id": tid, "workload_type": "oltp_read_write", "threads": 2,
        "tables": 4, "table_size": 1000, "label": "journey-247",
        "segment_time_s": 2, "prepare": True}, auth=("op", "oppw"))
    assert r.status_code == 200
    jid = r.json()["job_id"]

    # ── 3. the worker claims and launches the harness ──
    claimed = queries.claim_next_job(conn, 1, continuous_cap=4)
    assert claimed is not None and claimed["id"] == jid
    t = threading.Thread(target=worker._run_job_threaded, args=(cfg, store, jid),
                         daemon=True)
    t.start()
    job = _wait(lambda: (lambda j: j if j and j["run_id"] and j["pid"] else None)(
        queries.get_job(conn, jid)), what="run linked + pid")
    run_id = job["run_id"]
    run_dir = cfg.results_dir / run_id
    # prepare actually loaded the missing dataset (fake marker written)
    assert (Path(os.environ["FAKE_PSQL_STATE"]) / "prepared").exists()

    # ── 4. sysbench crash: the FIRST run exited network-refused; the
    #      supervisor relaunched with backoff and the load then flowed ──
    _wait(lambda: (run_dir / "events.jsonl").exists()
          and "loadgen_restart" in (run_dir / "events.jsonl").read_text(),
          what="loadgen_restart event")
    _wait(lambda: sum(1 for _ in (run_dir / "raw").glob("cont_seg*.log")) >= 2,
          what="post-crash segments")

    # ── 5. samples flow into the metrics pipeline ──
    _wait(lambda: contmetrics.ingest_job(cfg, conn, jid, run_id) or
          conn.execute("SELECT count(*) FROM cont_samples WHERE job_id=?",
                       (jid,)).fetchone()[0] >= 2, timeout_s=30,
          what="ingested samples")
    assert conn.execute("SELECT count(*) FROM cont_rollup_1m WHERE job_id=?",
                        (jid,)).fetchone()[0] >= 1
    ts = client.get(f"/api/continuous/{jid}/timeseries?window=10m",
                    auth=("op", "oppw")).json()
    assert ts["points"] >= 1 and ts["resolution"] == "1s"

    # ── 6. droplet reboot: everything dies at once; on boot the reconcile
    #      relaunches into the SAME run dir ──
    import signal
    os.kill(int(job["pid"]), signal.SIGKILL)          # harness dies uncleanly
    t.join(timeout=30)
    assert not t.is_alive()
    segs_before = len(list((run_dir / "raw").glob("cont_seg*.log")))
    # after the reboot the job row looks stale-running with a dead pid
    queries.update_job(conn, jid, state="running", pid=99999994, pid_start="")
    worker.reconcile_startup(cfg, conn)
    job = queries.get_job(conn, jid)
    assert job["state"] == "queued" and job["desired_state"] == "running"
    row = conn.execute("SELECT * FROM alerts WHERE job_id=? AND "
                       "type='harness_relaunch'", (jid,)).fetchone()
    assert row is not None and row["severity"] == "info"
    claimed = queries.claim_next_job(conn, 1, continuous_cap=4)
    assert claimed is not None and claimed["id"] == jid
    t = threading.Thread(target=worker._run_job_threaded, args=(cfg, store, jid),
                         daemon=True)
    t.start()
    _wait(lambda: len(list((run_dir / "raw").glob("cont_seg*.log"))) > segs_before,
          what="new segment in the SAME run dir")
    assert queries.get_job(conn, jid)["run_id"] == run_id

    # ── 7. probe failure → outage ledger; Slack delivery recorded (mocked) ──
    monkeypatch.setenv("FAKE_PSQL_PROBE_FAIL", "write")
    contprobe.probe_job_loop(cfg, jid, max_ticks=3)   # 2 failures -> down
    out = conn.execute("SELECT * FROM outages WHERE job_id=? AND kind='write'",
                       (jid,)).fetchone()
    assert out is not None and out["ended_utc"] is None
    contworker.engine_tick(cfg, conn, store, deliver_kwargs={
        "send": lambda _h, text: slack_sent.append(text) or True,
        "base_delay_s": 0.001})
    a = conn.execute("SELECT * FROM alerts WHERE job_id=? AND type='db_unreachable'",
                     (jid,)).fetchone()
    assert a is not None and a["delivery"] == "slack:ok" and a["delivered_utc"]
    assert any("db_unreachable" in s for s in slack_sent)
    # recovery closes the ledger row with a duration
    monkeypatch.delenv("FAKE_PSQL_PROBE_FAIL")
    contprobe.probe_job_loop(cfg, jid, max_ticks=2)
    out = conn.execute("SELECT * FROM outages WHERE id=?", (out["id"],)).fetchone()
    assert out["ended_utc"] is not None and out["duration_s"] is not None
    assert conn.execute("SELECT count(*) FROM alerts WHERE job_id=? AND "
                        "type='db_recovered'", (jid,)).fetchone()[0] == 1

    # ── 8. deliberate stop through the API: desired_state wins forever ──
    r = client.post(f"/api/continuous/{jid}/stop", auth=("op", "oppw"))
    assert r.status_code == 200
    t.join(timeout=30)
    assert not t.is_alive()
    job = queries.get_job(conn, jid)
    assert job["state"] == "canceled" and job["desired_state"] == "stopped"
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["status"] == "stopped"
    assert read_state(run_dir).get("status") == "stopped"

    # ── 9. NOTHING resurrects it: housekeeping and a fresh reconcile agree ──
    contworker.housekeeping(cfg, conn)
    worker.reconcile_startup(cfg, conn)
    job = queries.get_job(conn, jid)
    assert job["state"] == "canceled" and job["desired_state"] == "stopped"

    # ── 10. the secrets policy held through every phase ──
    leaks = [p for p in run_dir.rglob("*")
             if p.is_file() and PW.encode() in p.read_bytes()]
    assert not leaks
    assert PW not in json.dumps(dict(job))
    conn.close()
