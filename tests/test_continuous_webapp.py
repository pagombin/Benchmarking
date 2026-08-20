"""Continuous Mode HTTP API: RBAC, the saved-target-required rule, one-per-
target, stop/resume semantics, window validation, maintenance CRUD, and the
global alert/outage ledgers."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
FAKEBIN = TESTS / "fakebin"


@pytest.fixture()
def web(tmp_path, monkeypatch):
    for exe in ("sysbench", "psql"):
        p = FAKEBIN / exe
        p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    import sys
    venv_bin = Path(sys.executable).parent
    monkeypatch.setenv("PATH", f"{FAKEBIN}{os.pathsep}{venv_bin}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("PGBENCH_HARNESS_BIN", str(venv_bin / "pgbench-harness"))
    monkeypatch.setenv("PGB_PROBE_GRACE_S", "0.3")
    state = tmp_path / "fakestate"
    state.mkdir()
    monkeypatch.setenv("FAKE_PSQL_STATE", str(state))
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
    queries.create_user(conn, "viewer", hash_password("vpw"), "viewer")
    conn.close()
    client = TestClient(create_app(cfg))
    return client, cfg


def _mk_target(client, name="c1", password="pw-secret"):
    return client.post("/api/targets", json={
        "name": name, "host": "db.example.invalid", "port": 5432,
        "dbname": "sbtest", "dbuser": "doadmin", "password": password},
        auth=("op", "oppw"))


def test_create_requires_saved_target_with_password(web):
    client, _cfg = web
    # no target at all
    r = client.post("/api/continuous", json={"threads": 4}, auth=("op", "oppw"))
    assert r.status_code == 400 and "saved target" in r.json()["detail"]
    # per-job password is rejected outright with the durable-credential rationale
    tid = _mk_target(client).json()["id"]
    r = client.post("/api/continuous", json={"target_id": tid, "password": "x"},
                    auth=("op", "oppw"))
    assert r.status_code == 400 and "reboot relaunch" in r.json()["detail"]
    # a target saved WITHOUT a password is unusable for continuous
    tid2 = _mk_target(client, name="no-pw", password="").json()["id"]
    r = client.post("/api/continuous", json={"target_id": tid2}, auth=("op", "oppw"))
    assert r.status_code == 400 and "no stored password" in r.json()["detail"]
    # unknown target
    r = client.post("/api/continuous", json={"target_id": 9999}, auth=("op", "oppw"))
    assert r.status_code == 400


def test_create_stop_resume_lifecycle(web):
    client, cfg = web
    from pgbench_webapp import queries
    from pgbench_webapp.db import connect
    tid = _mk_target(client).json()["id"]
    r = client.post("/api/continuous",
                    json={"target_id": tid, "workload_type": "oltp_read_write",
                          "threads": 8, "tables": 4, "table_size": 1000,
                          "label": "247-adv", "prepare": True},
                    auth=("op", "oppw"))
    assert r.status_code == 200
    jid = r.json()["job_id"]
    conn = connect(cfg.db_path)
    job = queries.get_job(conn, jid)
    assert job["kind"] == "continuous" and job["desired_state"] == "running"
    assert json.loads(job["options"])["prepare"] is True
    assert "password" not in job["spec_yaml"].replace("password_env", "")
    conn.close()

    # one continuous workload per target
    r = client.post("/api/continuous", json={"target_id": tid}, auth=("op", "oppw"))
    assert r.status_code == 409 and "already has an active" in r.json()["detail"]

    # detail + list are viewer-visible
    d = client.get(f"/api/continuous/{jid}", auth=("viewer", "vpw")).json()
    assert d["desired_state"] == "running" and d["target_name"] == "c1"
    assert d["workload_type"] == "oltp_read_write" and d["threads"] == 8
    lst = client.get("/api/continuous", auth=("viewer", "vpw")).json()
    assert len(lst) == 1 and lst[0]["id"] == jid and "spark" in lst[0]

    # stop: durable intent set even for a queued job
    r = client.post(f"/api/continuous/{jid}/stop", auth=("op", "oppw"))
    assert r.status_code == 200 and r.json()["desired_state"] == "stopped"
    conn = connect(cfg.db_path)
    job = queries.get_job(conn, jid)
    assert job["desired_state"] == "stopped" and job["state"] == "canceled"
    conn.close()

    # resume re-queues the SAME job (same run dir once linked)
    r = client.post(f"/api/continuous/{jid}/resume", auth=("op", "oppw"))
    assert r.status_code == 200
    conn = connect(cfg.db_path)
    job = queries.get_job(conn, jid)
    assert job["state"] == "queued" and job["desired_state"] == "running"
    conn.close()
    # resuming an active job is a 409
    assert client.post(f"/api/continuous/{jid}/resume",
                       auth=("op", "oppw")).status_code == 409
    # a second workload on the target is still blocked (the resumed one is active)
    assert client.post("/api/continuous", json={"target_id": tid},
                       auth=("op", "oppw")).status_code == 409


def test_rbac_matrix_for_continuous(web):
    client, _cfg = web
    tid = _mk_target(client).json()["id"]
    body = {"target_id": tid}
    # viewer: read yes, mutate no
    assert client.post("/api/continuous", json=body, auth=("viewer", "vpw")).status_code == 403
    assert client.get("/api/continuous", auth=("viewer", "vpw")).status_code == 200
    assert client.get("/api/alerts", auth=("viewer", "vpw")).status_code == 200
    assert client.get("/api/outages", auth=("viewer", "vpw")).status_code == 200
    assert client.post("/api/maintenance", json={}, auth=("viewer", "vpw")).status_code == 403
    # unauthenticated: 401 across the board
    assert client.get("/api/continuous").status_code == 401
    assert client.get("/api/alerts").status_code == 401
    jid = client.post("/api/continuous", json=body, auth=("op", "oppw")).json()["job_id"]
    assert client.post(f"/api/continuous/{jid}/stop", auth=("viewer", "vpw")).status_code == 403
    assert client.post(f"/api/continuous/{jid}/resume", auth=("viewer", "vpw")).status_code == 403
    assert client.get(f"/api/continuous/{jid}/timeseries?window=1h",
                      auth=("viewer", "vpw")).status_code == 200
    # notify test stays admin-only and reports channels
    assert client.post("/api/notify/test", json={}, auth=("op", "oppw")).status_code == 403
    r = client.post("/api/notify/test", json={"text": "hello"}, auth=("admin", "apw"))
    assert r.status_code == 200 and set(r.json()["channels"]) == {"email", "slack"}


def test_window_validation(web):
    client, _cfg = web
    tid = _mk_target(client).json()["id"]
    jid = client.post("/api/continuous", json={"target_id": tid},
                      auth=("op", "oppw")).json()["job_id"]
    base = f"/api/continuous/{jid}"
    assert client.get(f"{base}/timeseries?window=3y", auth=("viewer", "vpw")).status_code == 400
    r = client.get(f"{base}/timeseries?from=2026-01-01T00:00:00Z&to=2026-03-01T00:00:00Z",
                   auth=("viewer", "vpw"))
    assert r.status_code == 400 and "31 days" in r.json()["detail"]
    r = client.get(f"{base}/timeseries?from=nonsense", auth=("viewer", "vpw"))
    assert r.status_code == 400 and "unrecognized" in r.json()["detail"]
    # an explicit custom range works through the documented `from`/`to` names
    assert client.get(f"{base}/timeseries?from=2026-01-01T00:00:00Z"
                      "&to=2026-01-02T00:00:00Z",
                      auth=("viewer", "vpw")).status_code == 200
    r = client.get(f"{base}/timeseries?window=10m", auth=("viewer", "vpw"))
    assert r.status_code == 200
    d = r.json()
    assert d["resolution"] == "1s" and d["t"] == []
    assert d["outages"] == [] and d["maintenance"] == []
    r = client.get(f"{base}/summary?window=24h", auth=("viewer", "vpw"))
    assert r.status_code == 200 and r.json()["uptime_pct"] == 100.0
    assert client.get(f"{base}/dbmetrics?window=1h",
                      auth=("viewer", "vpw")).status_code == 200
    # unknown job id -> 404, not 500
    assert client.get("/api/continuous/424242/timeseries?window=1h",
                      auth=("viewer", "vpw")).status_code == 404


def test_maintenance_crud(web):
    client, _cfg = web
    r = client.post("/api/maintenance",
                    json={"starts_utc": "2026-06-01T00:00:00Z",
                          "ends_utc": "2026-06-01T02:00:00Z",
                          "note": "planned failover drill"},
                    auth=("op", "oppw"))
    assert r.status_code == 200
    mid = r.json()["id"]
    rows = client.get("/api/maintenance", auth=("viewer", "vpw")).json()
    assert len(rows) == 1 and rows[0]["job_id"] is None
    # inverted range refused
    assert client.post("/api/maintenance",
                       json={"starts_utc": "2026-06-01T02:00:00Z",
                             "ends_utc": "2026-06-01T00:00:00Z"},
                       auth=("op", "oppw")).status_code == 400
    # job-scoped window needs a real continuous job
    assert client.post("/api/maintenance",
                       json={"job_id": 999, "starts_utc": "2026-06-01T00:00:00Z",
                             "ends_utc": "2026-06-01T01:00:00Z"},
                       auth=("op", "oppw")).status_code == 404
    assert client.delete(f"/api/maintenance/{mid}", auth=("op", "oppw")).json()["deleted"]
    assert client.delete(f"/api/maintenance/{mid}", auth=("op", "oppw")).status_code == 404


def test_global_ledgers_filter(web):
    client, cfg = web
    from pgbench_webapp import alerts
    from pgbench_webapp.db import connect
    tid = _mk_target(client).json()["id"]
    jid = client.post("/api/continuous", json={"target_id": tid},
                      auth=("op", "oppw")).json()["job_id"]
    conn = connect(cfg.db_path)
    alerts.store_alert(conn, type="db_unreachable", severity="crit",
                       dedup_key=f"job:{jid}:x", job_id=jid)
    alerts.store_alert(conn, type="latency", severity="warn",
                       dedup_key=f"job:{jid}:y", job_id=jid)
    conn.execute("INSERT INTO outages(job_id, kind, started_utc, planned) "
                 "VALUES (?,?,?,0)", (jid, "read", "2026-06-01T00:00:00Z"))
    conn.close()
    assert len(client.get("/api/alerts", auth=("viewer", "vpw")).json()) == 2
    crits = client.get("/api/alerts?severity=crit", auth=("viewer", "vpw")).json()
    assert len(crits) == 1 and crits[0]["type"] == "db_unreachable"
    assert len(client.get("/api/alerts?unresolved=1", auth=("viewer", "vpw")).json()) == 2
    assert client.get("/api/alerts?since=bogus", auth=("viewer", "vpw")).status_code == 400
    outs = client.get("/api/outages?open_only=1", auth=("viewer", "vpw")).json()
    assert len(outs) == 1 and outs[0]["kind"] == "read"
    assert client.get("/api/outages?kind=write", auth=("viewer", "vpw")).json() == []
    # per-job ledgers
    assert len(client.get(f"/api/continuous/{jid}/alerts?severity=warn",
                          auth=("viewer", "vpw")).json()) == 1
    assert len(client.get(f"/api/continuous/{jid}/outages",
                          auth=("viewer", "vpw")).json()) == 1


def test_api_runs_rejects_continuous_spec(web):
    client, _cfg = web
    spec = """run:
  label: sneaky
  edition: advanced
  tshirt_size: 4c16g
target:
  host: h
  port: 5432
  database: d
  user: u
  password_env: PGB_TARGET_PASSWORD
workload:
  type: oltp_read_write
  tables: 1
  table_size: 100
continuous:
  threads: 2
"""
    r = client.post("/api/runs", json={"spec_yaml": spec, "password": "x"},
                    auth=("op", "oppw"))
    assert r.status_code == 400 and "/api/continuous" in r.json()["detail"]


def test_admin_settings_roundtrip_continuous_knobs(web):
    client, _cfg = web
    r = client.get("/api/admin/settings", auth=("admin", "apw")).json()
    assert r["continuous_cap"] == 4 and r["heartbeat_url"] == ""
    assert r["cont_alerts_config"]["probe_interval_s"] == 5
    ok = client.post("/api/admin/settings", json={
        "continuous_cap": 2, "heartbeat_url": "https://hc.example/x",
        "cont_retention_raw_h": 48, "cont_retention_days": 30,
        "cont_alerts_config": {"latency_p99_ms": 250, "renotify_min": 30},
    }, auth=("admin", "apw"))
    assert ok.status_code == 200
    r = client.get("/api/admin/settings", auth=("admin", "apw")).json()
    assert r["continuous_cap"] == 2
    assert r["heartbeat_url"] == "https://hc.example/x"
    assert r["cont_retention_raw_h"] == 48
    assert r["cont_alerts_config"]["latency_p99_ms"] == 250
    assert r["cont_alerts_config"]["renotify_min"] == 30
    assert r["cont_alerts_config"]["probe_interval_s"] == 5   # defaults survive
    # validation: unknown key, non-positive value, bad URL scheme
    assert client.post("/api/admin/settings", json={
        "cont_alerts_config": {"nonsense": 1}}, auth=("admin", "apw")).status_code == 400
    assert client.post("/api/admin/settings", json={
        "cont_alerts_config": {"latency_p99_ms": -5}}, auth=("admin", "apw")).status_code == 400
    assert client.post("/api/admin/settings", json={
        "heartbeat_url": "ftp://nope"}, auth=("admin", "apw")).status_code == 400
    # viewer/operator cannot touch settings
    assert client.get("/api/admin/settings", auth=("op", "oppw")).status_code == 403
