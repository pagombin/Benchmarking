"""Cluster Ops: kube targets CRUD/RBAC, validate/discover jobs against the fake
kubectl, topology caching, ops-run indexing, and the extended secret-leak gate
(kubeconfig contents + k8s-secret-derived password).
"""

from __future__ import annotations

import json
import os
import stat
import warnings
from pathlib import Path

import pytest

warnings.filterwarnings("ignore")

TESTS = Path(__file__).resolve().parent
FAKEBIN = TESTS / "fakebin"

KUBE_TOKEN = "kube-token-SENTINEL-do-not-leak-abcdef123456"
K8S_PW = "k8s-pguser-password-SENTINEL-98765"

KUBECONFIG_CONTENT = f"""apiVersion: v1
kind: Config
clusters:
- cluster:
    certificate-authority-data: TFMwdExTMUNSVWRKVGlCRFJWSlVTVVpKUTBGVVJTMHRMUzB0
    server: https://fake-k8s.example.com:6443
  name: do-nyc1-fake
contexts:
- context: {{cluster: do-nyc1-fake, user: do-nyc1-fake-admin}}
  name: do-nyc1-fake
current-context: do-nyc1-fake
users:
- name: do-nyc1-fake-admin
  user:
    token: {KUBE_TOKEN}
"""


@pytest.fixture()
def opsweb(tmp_path, monkeypatch):
    """TestClient + cfg with the fake kubectl cluster on PATH."""
    for exe in ("sysbench", "psql", "kubectl"):
        p = FAKEBIN / exe
        p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    import sys
    venv_bin = Path(sys.executable).parent
    monkeypatch.setenv("PATH", f"{FAKEBIN}{os.pathsep}{venv_bin}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("PGBENCH_HARNESS_BIN", str(venv_bin / "pgbench-harness"))
    state = tmp_path / "fakekube"; state.mkdir()
    monkeypatch.setenv("FAKE_KUBE_STATE", str(state))
    monkeypatch.setenv("FAKE_KUBE_PGPASS", K8S_PW)
    # fast scenario/backup timings for tests
    monkeypatch.setenv("FAKE_KUBE_RESTART_S", "1.5")
    monkeypatch.setenv("FAKE_KUBE_BACKUP_S", "1.0")
    monkeypatch.setenv("FAKE_KUBE_LOG_FOLLOW_S", "6")
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
    from pgbench_webapp import queries, worker
    from pgbench_webapp.db import connect
    conn = connect(cfg.db_path)
    try:
        job = queries.claim_next_job(conn, 4)
        assert job is not None, "expected a queued job"
        state = worker.run_job(cfg, conn, job)
        return job["id"], state, queries.get_job(conn, job["id"])
    finally:
        conn.close()


def _drain_queue(cfg, max_jobs: int = 5):
    """Run queued jobs until the queue is empty; returns final states."""
    from pgbench_webapp import queries, worker
    from pgbench_webapp.db import connect
    states = []
    conn = connect(cfg.db_path)
    try:
        for _ in range(max_jobs):
            job = queries.claim_next_job(conn, 8)
            if job is None:
                break
            states.append(worker.run_job(cfg, conn, job))
    finally:
        conn.close()
    return states


def _create_target(client, upload: bool = True, **overrides):
    payload = {"name": "doks-test", "namespace": "percona", "db_user": "doadmin"}
    if upload:
        payload["kubeconfig_content"] = KUBECONFIG_CONTENT
    payload.update(overrides)
    r = client.post("/api/kube-targets", json=payload, auth=("admin", "apw"))
    assert r.status_code == 201, r.text
    return r.json()


# ── unit: patroni parser against real-format output ──

def test_patronictl_parse_real_format():
    from pgbench_harness.ops.patroni import parse_patronictl_list
    text = json.dumps([
        {"Cluster": "cluster1-ha", "Member": "cluster1-instance1-6kbw-0",
         "Host": "10.244.0.25", "Role": "Leader", "State": "running", "TL": 5},
        {"Cluster": "cluster1-ha", "Member": "cluster1-instance1-9zqp-0",
         "Host": "10.244.1.14", "Role": "Replica", "State": "streaming",
         "TL": 5, "Lag in MB": 0},
        {"Cluster": "cluster1-ha", "Member": "cluster1-instance1-x2vr-0",
         "Host": "10.244.2.8", "Role": "Sync Standby", "State": "streaming",
         "TL": 5, "Lag in MB": 12},
    ])
    view = parse_patronictl_list(text)
    assert view.leader_name == "cluster1-instance1-6kbw-0"
    assert view.timeline == 5
    assert len(view.members) == 3
    assert view.members[2].lag_mb == 12.0
    assert not view.members[1].is_leader


def test_patronictl_parse_rejects_garbage():
    from pgbench_harness.ops.patroni import parse_patronictl_list
    with pytest.raises(ValueError):
        parse_patronictl_list("{}")
    with pytest.raises(ValueError):
        parse_patronictl_list("[]")


def test_ops_spec_validation():
    from pgbench_harness.errors import SpecError
    from pgbench_harness.ops.opspec import parse_ops_spec
    with pytest.raises(SpecError):
        parse_ops_spec({"op": "nonsense", "target": {"name": "x"}})
    with pytest.raises(SpecError):    # scenario needs a case
        parse_ops_spec({"op": "scenario", "target": {"name": "x", "cr_name": "c"},
                        "params": {}})
    with pytest.raises(SpecError):    # destructive ops need a CR name
        parse_ops_spec({"op": "backup", "target": {"name": "x"},
                        "params": {"type": "full"}})
    spec = parse_ops_spec({"op": "discover", "target": {"name": "t"}})
    assert spec.target.namespace == "percona"
    assert spec.target.patroni_scope == "-ha"  # no cr yet


# ── kube target CRUD + RBAC ──

def test_kube_target_crud_rbac(opsweb):
    client, cfg = opsweb
    # viewer/operator cannot create
    r = client.post("/api/kube-targets", json={"name": "x", "kubeconfig_path": "/x"},
                    auth=("viewer", "vpw"))
    assert r.status_code == 403
    r = client.post("/api/kube-targets", json={"name": "x", "kubeconfig_path": "/x"},
                    auth=("op", "oppw"))
    assert r.status_code == 403
    created = _create_target(client)
    assert created["validate_job_id"]           # validation auto-enqueued
    # list: kubeconfig contents never surface; imported flag set
    r = client.get("/api/kube-targets", auth=("viewer", "vpw"))
    assert r.status_code == 200
    kt = r.json()[0]
    assert kt["kubeconfig_imported"] is True
    assert kt["kubeconfig_path"] == ""
    assert KUBE_TOKEN not in r.text
    # duplicate name refused
    r = client.post("/api/kube-targets",
                    json={"name": "doks-test", "kubeconfig_path": "/x"},
                    auth=("admin", "apw"))
    assert r.status_code == 409
    # update
    r = client.post(f"/api/kube-targets/{kt['id']}", json={"cr_name": "cluster1"},
                    auth=("admin", "apw"))
    assert r.status_code == 200 and r.json()["cr_name"] == "cluster1"
    # delete (queue is idle after we drain the auto-validate job)
    _drain_queue(cfg)
    r = client.delete(f"/api/kube-targets/{kt['id']}", auth=("admin", "apw"))
    assert r.status_code == 200
    assert client.get("/api/kube-targets", auth=("viewer", "vpw")).json() == []


def test_kube_target_requires_path_or_content(opsweb):
    client, _ = opsweb
    r = client.post("/api/kube-targets", json={"name": "nope"}, auth=("admin", "apw"))
    assert r.status_code == 400


# ── validate + discover through the worker ──

def test_validate_job_caches_summary(opsweb):
    client, cfg = opsweb
    created = _create_target(client)
    job_id, state, job = _run_worker_once(cfg)
    assert state == "done" and job_id == created["validate_job_id"]
    r = client.get(f"/api/kube-targets/{created['id']}", auth=("viewer", "vpw"))
    kt = r.json()
    assert kt["api_server"] == "https://fake-k8s.example.com:6443"
    assert kt["last_validated_utc"]
    assert kt["cr_name"] == "cluster1"          # auto-prefilled (single CR found)
    assert kt["pguser_secret"] == "cluster1-pguser-doadmin"
    # job stream shows structured checks
    r = client.get(f"/api/jobs/{job_id}", auth=("viewer", "vpw"))
    assert r.status_code == 200


def test_discover_caches_topology(opsweb):
    client, cfg = opsweb
    created = _create_target(client)
    _drain_queue(cfg)                            # auto-validate
    r = client.post(f"/api/kube-targets/{created['id']}/discover",
                    auth=("op", "oppw"))
    assert r.status_code == 200
    _drain_queue(cfg)
    topo = client.get(f"/api/kube-targets/{created['id']}/topology",
                      auth=("viewer", "vpw")).json()
    assert topo["topology"]["patroni"]["leader"] == "cluster1-instance1-abcd-0"
    assert topo["topology"]["cr_name"] == "cluster1"
    assert len(topo["topology"]["pods"]["instances"]) == 3
    assert topo["topology"]["backups"]["schedules"][0]["schedules"]["incremental"]
    assert topo["collected_utc"]


def test_validate_reports_missing_kubeconfig(opsweb):
    client, cfg = opsweb
    _create_target(client, upload=False, name="badpath",
                   kubeconfig_path="/nonexistent/kubeconfig.yaml")
    job_id, state, job = _run_worker_once(cfg)
    assert state == "failed"
    out = (cfg.data_dir / "jobs" / f"job_{job_id}.out").read_text()
    assert "not visible to the worker" in out
    assert "kubeconfigs/" in out                 # points at the sanctioned dir


# ── the extended leak gate ──

def test_kube_secrets_never_leak_anywhere(opsweb):
    """Kubeconfig contents and the k8s-secret-derived DB password must never
    appear in the DB, job specs, logs, API responses, or any artifact."""
    client, cfg = opsweb
    created = _create_target(client)
    _drain_queue(cfg)
    client.post(f"/api/kube-targets/{created['id']}/discover", auth=("op", "oppw"))
    _drain_queue(cfg)

    for sentinel in (KUBE_TOKEN, K8S_PW):
        for path in cfg.data_dir.rglob("*"):
            if not path.is_file():
                continue
            if path.name == "secrets.enc":       # Fernet ciphertext — checked below
                continue
            if "fakekube" in str(path):          # the fake cluster's own state
                continue
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            assert sentinel not in content, f"{sentinel[:12]}… leaked into {path}"
        enc = (cfg.data_dir / "secrets.enc").read_text()
        assert sentinel not in enc               # encrypted, not plaintext

    for url in ("/api/kube-targets", f"/api/kube-targets/{created['id']}",
                f"/api/kube-targets/{created['id']}/topology", "/api/ops/runs",
                "/api/jobs"):
        r = client.get(url, auth=("admin", "apw"))
        assert KUBE_TOKEN not in r.text and K8S_PW not in r.text, url


def test_ops_actions_audited(opsweb):
    client, cfg = opsweb
    created = _create_target(client)
    _drain_queue(cfg)
    client.post(f"/api/kube-targets/{created['id']}/discover", auth=("op", "oppw"))
    r = client.get("/api/audit", auth=("admin", "apw"))
    actions = [a["action"] for a in r.json()]
    assert "kube_target_create" in actions
    assert "ops_validate_enqueue" in actions
    assert "ops_discover_enqueue" in actions
