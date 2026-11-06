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
