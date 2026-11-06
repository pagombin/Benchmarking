"""MySQL engine support — harness core: spec, engines module, sysbench argv,
capture/preflight via the fake mysql client, live sampler mapping, and the
error classifier. MySQL must work exactly like PostgreSQL through the same
code paths, differing only where the engines module says so."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from pgbench_harness import capture, engines
from pgbench_harness.continuous import classify_error
from pgbench_harness.errors import PreflightError, SpecError
from pgbench_harness.spec import parse_spec
from pgbench_harness.sysbench import (
    build_prepare_command, build_run_command, child_env,
)

from conftest import TEST_PASSWORD, make_spec_doc


def mysql_doc(**overrides: object) -> dict:
    doc = make_spec_doc(**overrides)
    doc["target"]["engine"] = "mysql"
    doc["target"]["port"] = 3306
    return doc


# ── spec parsing ────────────────────────────────────────────────────

def test_engine_defaults_to_postgres() -> None:
    spec = parse_spec(make_spec_doc())
    assert spec.target.engine == "postgres"


def test_engine_mysql_parses_and_normalizes() -> None:
    assert parse_spec(mysql_doc()).target.engine == "mysql"
    doc = make_spec_doc()
    doc["target"]["engine"] = "PostgreSQL"
    assert parse_spec(doc).target.engine == "postgres"


def test_engine_rejects_unknown() -> None:
    doc = make_spec_doc()
    doc["target"]["engine"] = "oracle"
    with pytest.raises(SpecError, match="target.engine"):
        parse_spec(doc)


def test_mysql_password_still_forbidden_in_spec() -> None:
    doc = mysql_doc()
    doc["target"]["password"] = "nope"
    with pytest.raises(SpecError, match="never contain a password"):
        parse_spec(doc)


def test_mysql_refuses_pgbench_suite() -> None:
    doc = mysql_doc()
    doc.pop("sweep")
    doc.pop("report", None)
    doc["suite"] = {"threads": [1, 2], "duration_s": 60, "warmup_s": 5,
                    "cooldown_s": 0, "pgbench": True}
    with pytest.raises(SpecError, match="PostgreSQL-only"):
        parse_spec(doc)
    doc["suite"]["pgbench"] = False
    assert parse_spec(doc).target.engine == "mysql"


# ── sysbench command construction ───────────────────────────────────

def test_mysql_sysbench_argv_and_env() -> None:
    spec = parse_spec(mysql_doc())
    cmd = build_run_command(spec, threads=4)
    argv = " ".join(cmd.argv)
    assert "--db-driver=mysql" in argv
    assert "--mysql-host=db.example.invalid" in argv
    assert "--mysql-port=3306" in argv
    assert "--mysql-user=doadmin" in argv
    assert "--mysql-db=sbtest" in argv
    assert "--mysql-ssl=on" in argv
    # the secrets invariant holds for the mysql driver too
    assert TEST_PASSWORD not in argv
    assert "--mysql-password" not in argv
    env = child_env(spec, TEST_PASSWORD)
    assert env["MYSQL_PWD"] == TEST_PASSWORD
    assert "PGPASSWORD" not in env or env.get("PGPASSWORD") != TEST_PASSWORD


def test_mysql_ssl_disable_maps_to_off() -> None:
    doc = mysql_doc()
    doc["target"]["sslmode"] = "disable"
    cmd = build_prepare_command(parse_spec(doc))
    assert "--mysql-ssl=off" in cmd.argv


def test_postgres_argv_unchanged() -> None:
    spec = parse_spec(make_spec_doc())
    cmd = build_run_command(spec, threads=1)
    assert "--db-driver=pgsql" in cmd.argv
    env = child_env(spec, TEST_PASSWORD)
    assert env["PGPASSWORD"] == TEST_PASSWORD and env["PGSSLMODE"] == "require"


# ── engines module units ────────────────────────────────────────────

def test_client_argv_shapes() -> None:
    pg = engines.client_argv("postgres", "h", 5432, "u", "d", "SELECT 1")
    assert pg[0] == "psql" and "-c" in pg and "ON_ERROR_STOP=1" in pg
    my = engines.client_argv("mysql", "h", 3306, "u", "d", "SELECT 1",
                             sslmode="verify-full", connect_timeout_s=3,
                             statement_timeout_ms=3000)
    assert my[0] == "mysql" and "--batch" in my and "--skip-column-names" in my
    assert "--ssl-mode=VERIFY_IDENTITY" in my
    assert "--connect-timeout=3" in my
    assert any(a.startswith("--init-command=SET SESSION max_execution_time=3000")
               for a in my)
    assert my[-1] == "SELECT 1" and my[-2] == "-e"


def test_cell_sep_hold_and_canary_sql() -> None:
    assert engines.cell_sep("postgres") == "|"
    assert engines.cell_sep("mysql") == "\t"
    assert "pg_sleep(30)" in engines.hold_sql("postgres")
    assert engines.hold_sql("mysql") == "SELECT sleep(30)"
    assert "ON CONFLICT" in engines.canary_write_sql("postgres")
    assert "ON DUPLICATE KEY UPDATE" in engines.canary_write_sql("mysql")
    assert engines.quote_ident("mysql", "we`ird") == "`we``ird`"


# ── error classification ────────────────────────────────────────────

def test_classify_error_mysql_patterns() -> None:
    assert classify_error(
        "ERROR 1045 (28000): Access denied for user 'bench'@'1.2.3.4' "
        "(using password: YES)") == "auth"
    assert classify_error(
        "Host '10.0.0.9' is not allowed to connect to this MySQL server") == "auth"
    assert classify_error(
        "ERROR 2003 (HY000): Can't connect to MySQL server on 'db' (111)") == "network"
    assert classify_error(
        "ERROR 2013 (HY000): Lost connection to MySQL server during query") == "network"
    assert classify_error("MySQL server has gone away") == "network"
    # auth still wins over network wording
    assert classify_error(
        "Can't connect: Access denied for user 'x'@'y'") == "auth"


# ── capture / preflight through the fake mysql client ───────────────

def test_mysql_queries_and_versions(fake_env: Path) -> None:
    spec = parse_spec(mysql_doc())
    assert capture.psql_query(spec, TEST_PASSWORD, "SELECT 1") == "1"
    assert "8.0" in capture.psql_query(
        spec, TEST_PASSWORD, engines.server_version_sql("mysql"))
    assert capture.client_version(spec).startswith("mysql")
    # bad password text surfaces as a redacted PreflightError
    with pytest.raises(PreflightError, match="query failed"):
        import os
        os.environ["FAKE_MYSQL_EXIT_AUTH"] = "1"
        try:
            capture.psql_query(spec, TEST_PASSWORD, "SELECT 1")
        finally:
            os.environ.pop("FAKE_MYSQL_EXIT_AUTH", None)


def test_mysql_full_preflight(fake_env: Path) -> None:
    (fake_env / "prepared").write_text("1")
    spec = parse_spec(mysql_doc())
    pf = capture.run_preflight(spec, TEST_PASSWORD, logging.getLogger("t"))
    assert pf.server_version == "8.0.39"
    assert pf.max_connections == "500"
    assert pf.pooler_probe.startswith("n/a")
    assert pf.pg_stat_monitor is False
    assert pf.dataset is not None and pf.dataset.ok
    assert pf.probe is not None and pf.probe.ok


def test_mysql_preflight_steps_checklist(fake_env: Path) -> None:
    (fake_env / "prepared").write_text("1")
    spec = parse_spec(mysql_doc())
    events = list(capture.preflight_steps(spec, TEST_PASSWORD,
                                          logging.getLogger("t")))
    names = {e["name"]: e for e in events}
    assert names["Connectivity"]["status"] == "ok"
    assert names["Server version"]["detail"] == "8.0.39"
    assert "n/a on MySQL" in names["pg_stat_monitor"]["detail"]
    assert names["Dataset"]["status"] == "ok"


def test_mysql_dataset_mismatch_detected(fake_env: Path,
                                         monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_MYSQL_MAX_ID", "5")   # spec says 1000 rows
    spec = parse_spec(mysql_doc())
    chk = capture.check_dataset(spec, TEST_PASSWORD)
    assert chk.status == "mismatch"


def test_mysql_settings_dump_is_csv(fake_env: Path) -> None:
    spec = parse_spec(mysql_doc())
    csv_text = capture.capture_pg_settings(spec, TEST_PASSWORD)
    lines = csv_text.splitlines()
    assert lines[0] == "name,setting,unit,source"
    assert any(line.startswith("innodb_buffer_pool_size,") for line in lines)


def test_mysql_admin_ops(fake_env: Path) -> None:
    spec = parse_spec(mysql_doc())
    maint = capture.maintenance_db(spec, TEST_PASSWORD)
    assert maint == "mysql"
    assert capture.database_exists(spec, TEST_PASSWORD, maint)
    ok, _ = capture.create_database(spec, TEST_PASSWORD, maint)
    assert ok
    ok, _ = capture.drop_database(spec, TEST_PASSWORD, maint)
    assert ok
    assert capture.database_size_bytes(spec, TEST_PASSWORD) == 1073741824


# ── live sampler mapping ────────────────────────────────────────────

def test_mysql_live_sampler_maps_pg_vocabulary(fake_env: Path) -> None:
    import time
    spec = parse_spec(mysql_doc())
    a = capture.live_pg_query(spec, TEST_PASSWORD)
    assert a is not None
    a["_mono"] = time.monotonic()
    time.sleep(1.1)                     # fake counters grow with wall-clock
    b = capture.live_pg_query(spec, TEST_PASSWORD)
    assert b is not None
    b["_mono"] = a["_mono"] + 1.1
    row = capture.pg_delta_row(a, b, t=1.1)
    assert row["active"] == 8 and row["total_conn"] == 20
    assert row["commits_s"] and float(row["commits_s"]) > 0
    assert row["wal_mb_s"] != "" and float(row["wal_mb_s"]) > 0
    assert row["cache_hit_pct"] != "" and float(row["cache_hit_pct"]) > 90
    # keys with no MySQL analogue chart as gaps, never crash
    assert row["ckpt_timed_s"] == "" and row["repl_replay_lag_s"] == ""


# ── webapp: targets, continuous submission, probes, collector ───────

import os as _os
import stat as _stat
import sys as _sys

TESTS = Path(__file__).resolve().parent
FAKEBIN = TESTS / "fakebin"


@pytest.fixture()
def web(tmp_path, monkeypatch):
    for exe in ("sysbench", "psql", "mysql"):
        p = FAKEBIN / exe
        p.chmod(p.stat().st_mode | _stat.S_IXUSR | _stat.S_IXGRP | _stat.S_IXOTH)
    venv_bin = Path(_sys.executable).parent
    monkeypatch.setenv("PATH", f"{FAKEBIN}{_os.pathsep}{venv_bin}{_os.pathsep}{_os.environ['PATH']}")
    monkeypatch.setenv("PGBENCH_HARNESS_BIN", str(venv_bin / "pgbench-harness"))
    monkeypatch.setenv("PGB_PROBE_GRACE_S", "0.3")
    state = tmp_path / "fakestate"
    state.mkdir()
    monkeypatch.setenv("FAKE_PSQL_STATE", str(state))
    data = tmp_path / "data"
    monkeypatch.setenv("PGBENCH_DATA_DIR", str(data))
    monkeypatch.setenv("PGBENCH_DB", str(data / "pgbench.db"))

    from fastapi.testclient import TestClient
    from pgbench_webapp import admin, queries as q
    from pgbench_webapp.app import create_app
    from pgbench_webapp.config import load_config
    from pgbench_webapp.db import connect
    from pgbench_webapp.security import hash_password

    cfg = load_config()
    admin.create_admin("admin", "apw")
    conn = connect(cfg.db_path)
    q.create_user(conn, "op", hash_password("oppw"), "operator")
    conn.close()
    client = TestClient(create_app(cfg))
    return client, cfg


def _mk_mysql_target(client, name="my1", password="pw-secret"):
    return client.post("/api/targets", json={
        "name": name, "host": "db.example.invalid", "engine": "mysql",
        "dbname": "sbtest", "dbuser": "bench", "password": password},
        auth=("op", "oppw"))


def test_target_engine_roundtrip_and_validation(web):
    client, _cfg = web
    r = _mk_mysql_target(client)
    assert r.status_code == 200
    rows = client.get("/api/targets", auth=("op", "oppw")).json()
    row = next(t for t in rows if t["name"] == "my1")
    assert row["engine"] == "mysql"
    assert row["port"] == 3306          # engine-aware default when omitted
    # default engine stays postgres
    r = client.post("/api/targets", json={
        "name": "pg1", "host": "h", "password": "x"}, auth=("op", "oppw"))
    rows = client.get("/api/targets", auth=("op", "oppw")).json()
    assert next(t for t in rows if t["name"] == "pg1")["engine"] == "postgres"
    # invalid engine refused on create and update
    r = client.post("/api/targets", json={
        "name": "bad", "host": "h", "engine": "oracle"}, auth=("op", "oppw"))
    assert r.status_code == 400
    tid = row["id"]
    r = client.post(f"/api/targets/{tid}", json={"engine": "oracle"},
                    auth=("op", "oppw"))
    assert r.status_code == 400


def test_continuous_spec_carries_engine(web):
    client, cfg = web
    from pgbench_webapp.db import connect
    tid = _mk_mysql_target(client).json()["id"]
    r = client.post("/api/continuous", json={"target_id": tid, "threads": 2},
                    auth=("op", "oppw"))
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]
    conn = connect(cfg.db_path)
    spec_yaml = conn.execute("SELECT spec_yaml FROM jobs WHERE id=?",
                             (job_id,)).fetchone()["spec_yaml"]
    conn.close()
    assert "engine: mysql" in spec_yaml
    assert "pw-secret" not in spec_yaml


def test_run_probe_mysql(web, monkeypatch):
    from pgbench_webapp.contprobe import run_probe
    target = {"host": "db.example.invalid", "port": 3306, "user": "bench",
              "database": "sbtest", "sslmode": "require", "engine": "mysql"}
    ok, err = run_probe(target, "pw", "read")
    assert ok, err
    ok, err = run_probe(target, "pw", "write")
    assert ok, err
    monkeypatch.setenv("FAKE_MYSQL_PROBE_FAIL", "read")
    ok, err = run_probe(target, "pw", "read")
    assert not ok and classify_error(err) == "network"
    ok, err = run_probe(target, "pw", "write")   # write path unaffected
    assert ok, err


def test_collect_once_mysql(web):
    from pgbench_webapp.contcollect import collect_once
    target = {"host": "db.example.invalid", "port": 3306, "user": "bench",
              "database": "sbtest", "sslmode": "require", "engine": "mysql"}
    m = collect_once(target, "pw", shape_key="db.example.invalid")
    assert m["conn_active"] == 3 and m["conn_total"] == 9
    assert m["conn_idle_tx"] is None            # NULL -> blank field, kept row
    assert m["xact_commit"] > 0 and m["blks_hit"] > 0 and m["blks_read"] > 0
    assert m["wal_bytes"] > 0 and m["wal_records"] is None
    assert m["db_size"] == 1073741824
    assert m["dead_tup"] == 1234 and m["repl_count"] == 2
    assert "ckpt_timed" not in m                 # no MySQL checkpoint analogue
    assert m["top_queries"] and m["top_queries"][0]["calls"] == 50000


def test_preflight_task_spec_merges_engine(web):
    client, cfg = web
    from pgbench_webapp.db import connect
    tid = _mk_mysql_target(client, name="my2").json()["id"]
    r = client.post("/api/preflight", json={
        "target_id": tid,
        "spec_yaml": ("run: {label: t, edition: advanced, tshirt_size: 4c16g}\n"
                      "workload: {type: oltp_read_write, tables: 1, table_size: 100}\n"
                      "sweep: {threads: [1], duration_s: 5, warmup_s: 1}\n")},
        auth=("op", "oppw"))
    assert r.status_code == 200, r.text
    conn = connect(cfg.db_path)
    spec_yaml = conn.execute("SELECT spec_yaml FROM jobs ORDER BY id DESC "
                             "LIMIT 1").fetchone()["spec_yaml"]
    conn.close()
    assert "engine: mysql" in spec_yaml


MYSQL_CONT_SPEC = """\
run:
  label: cont-mysql
  edition: advanced
  tshirt_size: 4c16g
target:
  host: db.example.invalid
  port: 3306
  database: sbtest
  user: bench
  password_env: PGB_TARGET_PASSWORD
  sslmode: require
  engine: mysql
workload:
  type: oltp_read_write
  tables: 4
  table_size: 1000
continuous:
  threads: 2
  segment_time_s: 1
  segment_kill_grace_s: 2
"""


def test_worker_e2e_mysql_continuous(web, monkeypatch):
    """run_job drives the real harness CLI for a MySQL target: the mysql
    sysbench driver is selected, segments stream, and no secret reaches any
    artifact — the identical lifecycle PostgreSQL gets."""
    import threading
    import time as _time
    from pgbench_webapp import queries as q, worker
    from pgbench_webapp.db import connect
    from pgbench_webapp.secrets_store import SecretStore
    _client, cfg = web
    monkeypatch.setenv("FAKE_SYSBENCH_REALTIME", "1")
    conn = connect(cfg.db_path)
    store = SecretStore(cfg.secret_key_path, cfg.data_dir / "secrets.enc")
    store.set("target:m1:password", "pw-secret-mysql")
    tid = q.create_target(conn, "m1", "db.example.invalid", 3306, "sbtest",
                          "bench", "require", "target:m1:password",
                          engine="mysql")
    jid = q.enqueue_job(conn, "continuous", MYSQL_CONT_SPEC, tid, "op",
                        desired_state="running")
    claimed = q.claim_next_job(conn, 1, continuous_cap=4)
    assert claimed is not None and claimed["id"] == jid
    t = threading.Thread(target=worker._run_job_threaded, args=(cfg, store, jid),
                         daemon=True)
    t.start()

    def wait(pred, timeout_s=30.0):
        deadline = _time.monotonic() + timeout_s
        while _time.monotonic() < deadline:
            v = pred()
            if v:
                return v
            _time.sleep(0.2)
        raise AssertionError("condition not met in time")

    job = wait(lambda: (lambda j: j if j and j["run_id"] and j["pid"] else None)(
        q.get_job(conn, jid)))
    run_dir = cfg.results_dir / job["run_id"]
    wait(lambda: list(run_dir.glob("raw/cont_seg*.log")))
    assert worker.stop_job_process(cfg, conn, jid) is True
    t.join(timeout=30)
    assert not t.is_alive()
    assert q.get_job(conn, jid)["state"] == "canceled"
    # the harness logged the sysbench command it built: the mysql driver,
    # with the password in NO argv and NO artifact
    harness_log = (run_dir / "harness.log").read_text()
    assert "--db-driver=mysql" in harness_log
    assert "--mysql-ssl=on" in harness_log
    leaked = [p for p in run_dir.rglob("*") if p.is_file()
              and "pw-secret-mysql" in p.read_text(errors="ignore")]
    assert leaked == []
    conn.close()


def test_mysql_ssl_ca_autodetect_and_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """TLS trust setup rides the ENV, not argv: sysbench 1.0.20 rejects
    --mysql-ssl-ca, and its SSL context init fails without a reachable CA
    ('SSL_CTX_set_default_verify_paths failed'). client_env points
    SSL_CERT_FILE/DIR at the system bundle; PGB_MYSQL_SSL_CA overrides,
    empty leaves the env untouched; sslmode=disable sets nothing."""
    monkeypatch.delenv("PGB_MYSQL_SSL_CA", raising=False)
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    engines._SYSBENCH_HELP = "...mysql-ssl[=on|off]..."      # a 1.0.20 build
    args = engines.sysbench_connection_args("mysql", "h", 3306, "u", "d")
    assert not any(a.startswith("--mysql-ssl-ca") for a in args)  # 1.0.20-safe
    engines._SYSBENCH_HELP = "...mysql-ssl-ca=STRING..."      # a capable build
    args = engines.sysbench_connection_args("mysql", "h", 3306, "u", "d")
    if engines.mysql_ssl_ca():
        assert any(a.startswith("--mysql-ssl-ca=") for a in args)
    engines._SYSBENCH_HELP = None                              # restore probe
    env = engines.client_env("mysql", "pw")
    if engines.mysql_ssl_ca():                       # system bundle present
        assert env["SSL_CERT_FILE"] == engines.mysql_ssl_ca()
        assert env["SSL_CERT_DIR"]
    monkeypatch.setenv("PGB_MYSQL_SSL_CA", "/custom/provider-ca.pem")
    env = engines.client_env("mysql", "pw")
    assert env["SSL_CERT_FILE"] == "/custom/provider-ca.pem"
    monkeypatch.setenv("PGB_MYSQL_SSL_CA", "")
    env = engines.client_env("mysql", "pw")
    assert "SSL_CERT_FILE" not in env
    monkeypatch.delenv("PGB_MYSQL_SSL_CA", raising=False)
    env = engines.client_env("mysql", "pw", sslmode="disable")
    assert "SSL_CERT_FILE" not in env
    # an operator's own SSL_CERT_FILE is never clobbered
    monkeypatch.setenv("SSL_CERT_FILE", "/operator/own.pem")
    env = engines.client_env("mysql", "pw")
    assert env["SSL_CERT_FILE"] == "/operator/own.pem"
