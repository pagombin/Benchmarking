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

# MySQL: the same engine-neutral field names, sourced from
# performance_schema.global_status / information_schema. blks_hit is derived
# (buffer-pool requests minus physical reads) so the console's cache-hit%
# math — hit/(hit+read) — stays the buffer-pool hit ratio. dead_tup maps to
# the InnoDB history list length (the purge-lag/bloat analogue). Fields with
# no MySQL equivalent select NULL and land as blanks, never blank rows.
_GS = ("(SELECT variable_value+0 FROM performance_schema.global_status "
       "WHERE variable_name='{v}')")
_MYSQL_QUERIES: dict[str, tuple[str, tuple[str, ...]]] = {
    "activity": (
        "SELECT " + _GS.format(v="Threads_running") + ", "
        + _GS.format(v="Threads_connected") + " - " + _GS.format(v="Threads_running")
        + ", NULL, " + _GS.format(v="Threads_connected"),
        ("conn_active", "conn_idle", "conn_idle_tx", "conn_total")),
    "database": (
        "SELECT " + _GS.format(v="Com_commit") + ", "
        + _GS.format(v="Com_rollback") + ", "
        + _GS.format(v="Innodb_buffer_pool_read_requests") + " - "
        + _GS.format(v="Innodb_buffer_pool_reads") + ", "
        + _GS.format(v="Innodb_buffer_pool_reads") + ", NULL, NULL",
        ("xact_commit", "xact_rollback", "blks_hit", "blks_read",
         "deadlocks", "temp_bytes")),
    "wal": (
        "SELECT NULL, " + _GS.format(v="Innodb_os_log_written"),
        ("wal_records", "wal_bytes")),
    "replication": (
        "SELECT NULL, (SELECT count(*) FROM information_schema.processlist "
        "WHERE command LIKE 'Binlog Dump%')",
        ("repl_lag_s", "repl_count")),
    "size": (
        "SELECT coalesce(sum(data_length + index_length), 0) "
        "FROM information_schema.tables WHERE table_schema = database()",
        ("db_size",)),
    "dead_tuples": (
        "SELECT coalesce((SELECT count FROM information_schema.innodb_metrics "
        "WHERE name='trx_rseg_history_len'), 0)",
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
DIGEST_EXISTS_SQL = ("SELECT count(*) FROM information_schema.tables "
                     "WHERE table_schema='performance_schema' "
                     "AND table_name='events_statements_summary_by_digest'")
DIGEST_TOP_SQL = ("SELECT left(regexp_replace(coalesce(digest_text, ''), "
                  "'\\\\s+', ' '), 80), count_star, round(sum_timer_wait/1e9) "
                  "FROM performance_schema.events_statements_summary_by_digest "
                  "ORDER BY sum_timer_wait DESC LIMIT 5")


def _psql(target: dict[str, Any], password: str, sql: str,
          timeout_s: float = 8.0) -> Optional[list[list[str]]]:
    """One split query -> rows of separated cells; None on any failure.

    Engine-aware (psql or mysql client); the cell separator differs per
    engine and the caller never sees it — rows come back pre-split."""
    from pgbench_harness import engines
    engine = engines.spec_target_engine(target)
    argv = engines.client_argv(
        engine, str(target.get("host", "")),
        int(target.get("port", engines.DEFAULT_PORTS.get(engine, 5432)) or 0),
        str(target.get("user", "")), str(target.get("database", "")), sql,
        sslmode=str(target.get("sslmode", "require")),
        connect_timeout_s=3, statement_timeout_ms=5000)
    env = engines.client_env(engine, password,
                             sslmode=str(target.get("sslmode", "require")),
                             connect_timeout_s=3, statement_timeout_ms=5000)
    try:
        proc = subprocess.run(argv, env=env, capture_output=True, text=True,
                              timeout=timeout_s)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    sep = engines.cell_sep(engine)
    return [line.split(sep) for line in proc.stdout.strip().splitlines()]


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
    from pgbench_harness import engines
    get_redactor().register(password)
    engine = engines.spec_target_engine(target)
    out: dict[str, Any] = {}
    queries_map = _MYSQL_QUERIES if engine == "mysql" else _QUERIES
    for _name, (sql, fields) in queries_map.items():
        rows = _psql(target, password, sql)
        if rows and rows[0]:
            for i, f in enumerate(fields):
                out[f] = _num(rows[0][i]) if i < len(rows[0]) else None
    if engine == "mysql":
        # no checkpoint-counter analogue; per-statement stats come from the
        # performance_schema digest table when it exists
        digest = _psql(target, password, DIGEST_EXISTS_SQL)
        if digest and digest[0] and _num(digest[0][0]):
            top = _psql(target, password, DIGEST_TOP_SQL)
            if top:
                out["top_queries"] = [
                    {"q": r[0], "calls": _num(r[1]) or 0, "total_ms": _num(r[2]) or 0}
                    for r in top if len(r) >= 3]
        return out
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
