"""DB-side metrics collector for Continuous Mode.

Every ``cont_collector_interval_s`` (default 15s) per running continuous job,
a set of SPLIT queries runs over psql against the target (works for any
managed PG endpoint — no kube required) and one compact JSON snapshot lands in
``cont_db_metrics``. The split-query lesson from ops/monitor.py applies: one
failing query leaves a BLANK FIELD, never a blank row. Raw counters are stored
(xact_commit, wal_bytes, ...); the API computes rates from consecutive rows.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from typing import Any, Optional

from pgbench_harness.util import get_redactor
from pgbench_webapp import queries
from pgbench_webapp.config import Config

COLLECTOR_INTERVAL_KEY = "cont_collector_interval_s"
COLLECTOR_INTERVAL_DEFAULT = 15

# name -> (sql, field names in select order). Every query is independent.
_QUERIES: dict[str, tuple[str, tuple[str, ...]]] = {
    "activity": (
        "SELECT count(*) FILTER (WHERE state='active'), "
        "count(*) FILTER (WHERE state='idle'), "
        "count(*) FILTER (WHERE state LIKE 'idle in%'), count(*) "
        "FROM pg_stat_activity",
        ("conn_active", "conn_idle", "conn_idle_tx", "conn_total")),
    "database": (
        "SELECT xact_commit, xact_rollback, blks_hit, blks_read, deadlocks, "
        "temp_bytes FROM pg_stat_database WHERE datname = current_database()",
        ("xact_commit", "xact_rollback", "blks_hit", "blks_read",
         "deadlocks", "temp_bytes")),
    "wal": (
        "SELECT wal_records, wal_bytes FROM pg_stat_wal",
        ("wal_records", "wal_bytes")),
    "archiver": (
        "SELECT archived_count, failed_count FROM pg_stat_archiver",
        ("archived_count", "archive_failed")),
    "replication": (
        "SELECT coalesce(max(extract(epoch from replay_lag)), 0), count(*) "
        "FROM pg_stat_replication",
        ("repl_lag_s", "repl_count")),
    "size": (
        "SELECT pg_database_size(current_database())",
        ("db_size",)),
    "dead_tuples": (
        "SELECT coalesce(sum(n_dead_tup), 0) FROM pg_stat_user_tables",
        ("dead_tup",)),
}
CKPT_SQL_17 = ("SELECT num_timed, num_requested FROM pg_stat_checkpointer",
               ("ckpt_timed", "ckpt_req"))
CKPT_SQL_OLD = ("SELECT checkpoints_timed, checkpoints_req FROM pg_stat_bgwriter",
                ("ckpt_timed", "ckpt_req"))
PGSS_EXISTS_SQL = "SELECT count(*) FROM pg_extension WHERE extname='pg_stat_statements'"
PGSS_TOP_SQL = ("SELECT left(regexp_replace(query, '\\s+', ' ', 'g'), 80), "
                "calls, round(total_exec_time)::bigint "
                "FROM pg_stat_statements ORDER BY total_exec_time DESC LIMIT 5")


def _psql(target: dict[str, Any], password: str, sql: str,
          timeout_s: float = 8.0) -> Optional[list[list[str]]]:
    """One split query -> rows of '|'-separated cells; None on any failure."""
    argv = ["psql", "-h", str(target.get("host", "")),
            "-p", str(target.get("port", 5432)),
            "-U", str(target.get("user", "")),
            "-d", str(target.get("database", "")),
            "-X", "-A", "-t", "-v", "ON_ERROR_STOP=1", "-c", sql]
    env = dict(os.environ)
    env["PGPASSWORD"] = password
    env["PGSSLMODE"] = str(target.get("sslmode", "require"))
    env["PGCONNECT_TIMEOUT"] = "3"
    env["PGOPTIONS"] = "-c statement_timeout=5000"
    try:
        proc = subprocess.run(argv, env=env, capture_output=True, text=True,
                              timeout=timeout_s)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    return [line.split("|") for line in proc.stdout.strip().splitlines()]


def _num(v: str) -> Any:
    try:
        f = float(v)
        return int(f) if f.is_integer() else f
    except (TypeError, ValueError):
        return None


# Latched per-process: which checkpoint stats shape this server speaks. Only
# latch onto the pre-17 shape when it actually RETURNS data (a transient empty
# answer on PG17 must not blank every later checkpoint sample — the monitor.py
# field lesson).
_ckpt_shape: dict[str, str] = {}


def collect_once(target: dict[str, Any], password: str,
                 shape_key: str = "") -> dict[str, Any]:
    """One collector pass: every split query, blank fields on failure."""
    get_redactor().register(password)
    out: dict[str, Any] = {}
    for _name, (sql, fields) in _QUERIES.items():
        rows = _psql(target, password, sql)
        if rows and rows[0]:
            for i, f in enumerate(fields):
                out[f] = _num(rows[0][i]) if i < len(rows[0]) else None
    shape = _ckpt_shape.get(shape_key, "17")
    sql, fields = CKPT_SQL_17 if shape == "17" else CKPT_SQL_OLD
    rows = _psql(target, password, sql)
    if not rows and shape == "17":
        old_rows = _psql(target, password, CKPT_SQL_OLD[0])
        if old_rows:
            _ckpt_shape[shape_key] = "old"
            rows, fields = old_rows, CKPT_SQL_OLD[1]
    if rows and rows[0]:
        for i, f in enumerate(fields):
            out[f] = _num(rows[0][i]) if i < len(rows[0]) else None
    pgss = _psql(target, password, PGSS_EXISTS_SQL)
    if pgss and pgss[0] and _num(pgss[0][0]):
        top = _psql(target, password, PGSS_TOP_SQL)
        if top:
            out["top_queries"] = [
                {"q": r[0], "calls": _num(r[1]) or 0, "total_ms": _num(r[2]) or 0}
                for r in top if len(r) >= 3]
    return out


_last_collect: dict[int, float] = {}


def collector_tick(cfg: Config, conn: sqlite3.Connection, store: Any) -> int:
    """Collect a snapshot for every running continuous job whose interval has
    elapsed. Returns the number of snapshots stored."""
    from pgbench_webapp.contprobe import job_target
    try:
        interval = int(queries.get_setting(conn, COLLECTOR_INTERVAL_KEY,
                                           str(COLLECTOR_INTERVAL_DEFAULT))
                       or COLLECTOR_INTERVAL_DEFAULT)
    except (TypeError, ValueError):
        interval = COLLECTOR_INTERVAL_DEFAULT
    interval = max(5, interval)
    stored = 0
    for job in conn.execute("SELECT * FROM jobs WHERE kind='continuous' "
                            "AND state='running'"):
        jid = int(job["id"])
        if time.monotonic() - _last_collect.get(jid, 0.0) < interval:
            continue
        resolved = job_target(conn, store, job)
        if resolved is None:
            continue
        _last_collect[jid] = time.monotonic()
        target, pw = resolved
        metrics = collect_once(target, pw, shape_key=str(target.get("host", "")))
        if not metrics:
            continue                # target fully unreachable: the prober owns that story
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            conn.execute(
                "INSERT OR REPLACE INTO cont_db_metrics(job_id, ts_utc, metrics) "
                "VALUES (?,?,?)", (jid, ts, json.dumps(metrics)))
            stored += 1
        except sqlite3.Error:
            continue
    return stored


def _collector_loop(cfg: Config) -> None:
    from pgbench_webapp.db import connect
    from pgbench_webapp.secrets_store import SecretStore
    conn = connect(cfg.db_path)
    store = SecretStore(cfg.secret_key_path, cfg.data_dir / "secrets.enc")
    while True:
        try:
            collector_tick(cfg, conn, store)
        except Exception:  # noqa: BLE001 — the collector must outlive any one bad tick
            pass
        time.sleep(5.0)
