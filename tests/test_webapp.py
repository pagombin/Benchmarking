"""Web app: auth/RBAC, migrations, queue + worker e2e (through the API, against
fake sysbench/psql), reports, soak+mark, audit, and the extended secrets-leak gate.
"""

from __future__ import annotations

import os
import stat
import warnings
from pathlib import Path

import pytest

warnings.filterwarnings("ignore")

TESTS = Path(__file__).resolve().parent
FAKEBIN = TESTS / "fakebin"
WEB_PW = "web-db-secret-DO-NOT-LEAK-42"


def _spec_yaml(mode: str = "sweep") -> str:
    base = """run:
  label: web-test
  edition: advanced
  tshirt_size: 4c16g
  tags: [ci, web]
  ticket: DBAAS-9
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
"""
    if mode == "soak":
        base += "soak:\n  threads: 2\n  duration_s: 2\n  tolerate_errors: true\n"
        base += "events:\n  - {at_s: 1, type: failover, label: fail}\n"
    else:
        base += "sweep:\n  threads: [1]\n  duration_s: 2\n  warmup_s: 1\n  cooldown_s: 0\n  repetitions: 1\n"
    return base


@pytest.fixture()
def web(tmp_path, monkeypatch):
    """A TestClient + cfg with fake sysbench/psql on PATH and three RBAC users."""
    for exe in ("sysbench", "psql"):
        p = FAKEBIN / exe
        p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    import sys
    venv_bin = Path(sys.executable).parent
    monkeypatch.setenv("PATH", f"{FAKEBIN}{os.pathsep}{venv_bin}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("PGBENCH_HARNESS_BIN", str(venv_bin / "pgbench-harness"))
    monkeypatch.setenv("PGB_PROBE_GRACE_S", "0.3")
    state = tmp_path / "fakestate"; state.mkdir()
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


def _run_worker_once(cfg):
    """Claim and execute one queued job synchronously (a single worker tick)."""
    from pgbench_webapp import queries, worker
    from pgbench_webapp.db import connect
    conn = connect(cfg.db_path)
    try:
        job = queries.claim_next_job(conn, 1)
        assert job is not None, "expected a queued job"
        state = worker.run_job(cfg, conn, job)
        return job["id"], state, queries.get_job(conn, job["id"])
    finally:
        conn.close()


# ── migrations ──────────────────────────────────────────────────────

def test_migrations_idempotent(tmp_path):
    from pgbench_webapp.db import migrate
    db = tmp_path / "m.db"
    assert migrate(db) >= 1          # applies on a fresh DB
    assert migrate(db) == 0          # no-op on re-run


def test_health_no_auth(web):
    client, _ = web
    assert client.get("/healthz").json()["status"] == "ok"


# ── auth / RBAC ─────────────────────────────────────────────────────

def test_rbac_matrix(web):
    client, _ = web
    spec = _spec_yaml()
    # viewer: can validate, cannot start/cancel/admin
    assert client.post("/api/validate", json={"spec_yaml": spec}, auth=("viewer", "vpw")).status_code == 200
    assert client.post("/api/runs", json={"spec_yaml": spec}, auth=("viewer", "vpw")).status_code == 403
    assert client.get("/admin/users", auth=("viewer", "vpw")).status_code == 403
    assert client.get("/audit", auth=("op", "oppw")).status_code == 403          # operator not admin
    # operator: can start; admin: can admin
    assert client.get("/admin/users", auth=("admin", "apw")).status_code == 200
    # unauthenticated
    assert client.post("/api/validate", json={"spec_yaml": spec}).status_code == 401


def test_bad_login_audited_and_rate_limited(web):
    client, cfg = web
    from pgbench_webapp.db import connect
    from pgbench_webapp import queries
    r = client.post("/login", data={"username": "admin", "password": "wrong"})
    assert "Invalid credentials" in r.text
    conn = connect(cfg.db_path)
    actions = [row["action"] for row in queries.list_audit(conn)]
    conn.close()
    assert "login_failed" in actions


# ── queue + worker e2e (through the API) ────────────────────────────

def test_run_through_api_and_worker(web):
    client, cfg = web
    r = client.post("/api/runs", json={"spec_yaml": _spec_yaml(), "password": WEB_PW},
                    auth=("op", "oppw"))
    assert r.status_code == 200
    job_id = r.json()["job_id"]
    jid, state, job = _run_worker_once(cfg)
    assert jid == job_id and state == "done"
    run_id = job["run_id"]
    assert run_id and (cfg.results_dir / run_id / "manifest.json").exists()
    # appears in history + report renders + artifact downloads
    assert run_id in client.get("/", auth=("viewer", "vpw")).text
    rep = client.get(f"/runs/{run_id}/report", auth=("viewer", "vpw"))
    assert rep.status_code == 200 and "Headline results" in rep.text
    art = client.get(f"/runs/{run_id}/artifact", auth=("viewer", "vpw"))
    assert art.status_code == 200 and art.headers["content-type"] == "application/gzip"
    # SSE stream of a finished run terminates with a done event
    body = client.get(f"/runs/{run_id}/stream", auth=("viewer", "vpw")).text
    assert "event: done" in body


def test_soak_through_api_with_mark(web):
    client, cfg = web
    r = client.post("/api/runs", json={"spec_yaml": _spec_yaml("soak"), "password": WEB_PW},
                    auth=("op", "oppw"))
    assert r.status_code == 200 and r.json()["kind"] == "soak"
    _, state, job = _run_worker_once(cfg)
    run_id = job["run_id"]
    assert run_id and (cfg.results_dir / run_id / "parsed" / "soak_summary.json").exists()
    # mark an event, then the (re)generated soak report includes the resilience section
    assert client.post(f"/api/runs/{run_id}/mark", json={"type": "failover", "label": "x"},
                       auth=("op", "oppw")).status_code == 200
    rep = client.get(f"/runs/{run_id}/report?regen=1", auth=("op", "oppw"))
    assert "Resilience" in rep.text


def test_cancel_queued_job(web):
    client, cfg = web
    job_id = client.post("/api/runs", json={"spec_yaml": _spec_yaml()}, auth=("op", "oppw")).json()["job_id"]
    assert client.post(f"/api/jobs/{job_id}/cancel", auth=("op", "oppw")).json()["canceled"] is True
    from pgbench_webapp import queries
    from pgbench_webapp.db import connect
    conn = connect(cfg.db_path)
    assert queries.get_job(conn, job_id)["state"] == "canceled"
    conn.close()


# ── secrets-leak gate (extended across web/DB/logs/audit/API) ───────

def test_secret_never_leaks_anywhere(web):
    client, cfg = web
    client.post("/api/runs", json={"spec_yaml": _spec_yaml(), "password": WEB_PW}, auth=("op", "oppw"))
    _run_worker_once(cfg)
    needle = WEB_PW.encode()
    leaks = []
    for p in cfg.data_dir.rglob("*"):
        if p.is_file() and p.name != "secrets.enc":      # secrets.enc is Fernet-encrypted
            if needle in p.read_bytes():
                leaks.append(str(p))
    assert not leaks, f"plaintext secret leaked into: {leaks}"
    # encrypted store must NOT contain the plaintext either
    enc = cfg.data_dir / "secrets.enc"
    if enc.exists():
        assert needle not in enc.read_bytes()
    # API responses must not echo it
    from pgbench_webapp import queries
    from pgbench_webapp.db import connect
    conn = connect(cfg.db_path)
    run_id = queries.list_runs(conn)[0]["run_id"]
    conn.close()
    for url in ("/", f"/runs/{run_id}", f"/runs/{run_id}/spec", f"/runs/{run_id}/report"):
        assert needle not in client.get(url, auth=("admin", "apw")).content


# ── reconciliation: CLI-created runs show up ────────────────────────

def test_reconcile_indexes_filesystem(web, tmp_path):
    client, cfg = web
    # simulate a run created directly by the CLI (outside the UI)
    rd = cfg.results_dir / "cli-made-20260101T000000Z"
    rd.mkdir(parents=True)
    (rd / "manifest.json").write_text(
        '{"run_id":"cli-made-20260101T000000Z","label":"cli","edition":"standard",'
        '"tshirt_size":"4c16g","mode":"sweep","status":"complete","created_utc":"2026-01-01T00:00:00Z"}')
    from pgbench_webapp import index, queries
    from pgbench_webapp.db import connect
    conn = connect(cfg.db_path)
    index.reconcile(conn, cfg.results_dir)
    assert queries.get_run(conn, "cli-made-20260101T000000Z") is not None
    conn.close()
