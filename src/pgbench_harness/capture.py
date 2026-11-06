"""Database/environment capture and preflight checks (via the engine's client).

The SQL client (psql for PostgreSQL, mysql for MySQL) is invoked with the
password in the engine's environment variable (``PGPASSWORD`` / ``MYSQL_PWD``)
and TLS/timeout settings via env or non-secret flags; no secret ever appears
on a command line or in a stored file. Function names keep their historical
``psql_*`` spelling — they are the harness's generic "run SQL on the target"
layer and dispatch on ``spec.target.engine``.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Optional

from pgbench_harness import __version__, engines
from pgbench_harness.errors import PreflightError
from pgbench_harness.spec import Spec
from pgbench_harness.sysbench import child_env, sysbench_version
from pgbench_harness.util import atomic_write_text, get_redactor

PSQL_TIMEOUT_S = 30
# How long the ceiling probe waits for all holders to establish before
# declaring success. Override (e.g. for tests/slow links) via env var.
PROBE_CONNECT_GRACE_S = float(os.environ.get("PGB_PROBE_GRACE_S", "6.0"))
PROBE_HOLD_SQL = "SELECT pg_sleep(30)"   # engine-dispatched at call time

KEY_SETTINGS = [
    "shared_buffers",
    "effective_cache_size",
    "max_wal_size",
    "checkpoint_timeout",
    "synchronous_commit",
    "max_connections",
    "work_mem",
    "random_page_cost",
    "wal_buffers",
    "huge_pages",
]


@dataclass
class ProbeResult:
    """Outcome of the connection-ceiling probe."""

    requested: int
    succeeded: int
    first_failed_index: Optional[int] = None
    first_error: str = ""

    @property
    def ok(self) -> bool:
        return self.succeeded >= self.requested


@dataclass
class DatasetCheck:
    """Outcome of the dataset conformance check (presence AND size vs spec)."""

    status: str = "error"  # ok | missing | wrong_schema | incomplete | mismatch | error
    detail: str = ""
    expected_tables: int = 0
    present_tables: int = 0
    foreign_tables: int = 0
    expected_size: int = 0
    actual_size: Optional[int] = None
    size_unit: str = ""
    found_elsewhere: list[str] = field(default_factory=list)
    search_path: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "ok"


@dataclass
class PreflightResult:
    """Everything preflight learned, recorded into the run manifest/env."""

    sysbench_version: str = ""
    psql_version: str = ""
    tpcc_git_sha: str = ""
    server_version_full: str = ""
    server_version: str = ""
    max_connections: str = ""
    pooler_probe: str = ""
    pg_stat_monitor: bool = False
    dataset: Optional[DatasetCheck] = None
    probe: Optional[ProbeResult] = None
    warnings: list[str] = field(default_factory=list)


def _psql_argv(spec: Spec, sql: str) -> list[str]:
    t = spec.target
    return engines.client_argv(t.engine, t.host, t.port, t.user, t.database,
                               sql, sslmode=t.sslmode)


def psql_query(spec: Spec, password: str, sql: str, timeout: int = PSQL_TIMEOUT_S) -> str:
    """Run one SQL statement via the engine's client and return trimmed stdout.

    Raises PreflightError with the verbatim (redacted) server error on failure.
    """
    client = engines.client_binary(spec.target.engine)
    try:
        proc = subprocess.run(
            _psql_argv(spec, sql),
            env=child_env(spec, password),
            capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise PreflightError(
            f"{client} is not installed or not on PATH",
            hint=engines.client_install_hint(spec.target.engine),
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise PreflightError(
            f"{client} timed out after {timeout}s connecting to "
            f"{spec.target.host}:{spec.target.port}",
            hint="check host/port, VPC reachability and firewall rules.",
        ) from exc
    if proc.returncode != 0:
        err = get_redactor().redact(proc.stderr.strip())
        raise PreflightError(
            f"{client} query failed against {spec.target.host}:{spec.target.port}: {err}",
            hint="verify credentials (password_env), database name and sslmode.",
        )
    return proc.stdout.strip()


def psql_query_soft(spec: Spec, password: str, sql: str,
                    timeout: int = PSQL_TIMEOUT_S) -> tuple[bool, str]:
    """Like psql_query but returns (ok, output_or_error) instead of raising."""
    try:
        return True, psql_query(spec, password, sql, timeout=timeout)
    except PreflightError as exc:
        return False, str(exc)


def psql_version() -> str:
    """Return `psql --version` output, raising PreflightError if missing."""
    try:
        out = subprocess.run(["psql", "--version"], capture_output=True, text=True, timeout=30)
    except FileNotFoundError as exc:
        raise PreflightError(
            "psql is not installed or not on PATH",
            hint="apt-get install postgresql-client (see README).",
        ) from exc
    return out.stdout.strip()


def client_version(spec: Spec) -> str:
    """Version of the engine's SQL client (psql / mysql), raising if missing."""
    if spec.target.engine != "mysql":
        return psql_version()
    try:
        out = subprocess.run(["mysql", "--version"], capture_output=True,
                             text=True, timeout=30)
    except FileNotFoundError as exc:
        raise PreflightError(
            "mysql client is not installed or not on PATH",
            hint=engines.client_install_hint("mysql"),
        ) from exc
    return out.stdout.strip()


def tpcc_git_sha(tpcc_path: str) -> str:
    """Return the git SHA of the sysbench-tpcc checkout (or a marker if unknown)."""
    if not tpcc_path:
        return "n/a (oltp workload)"
    try:
        out = subprocess.run(
            ["git", "-C", tpcc_path, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=30,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return "unknown (not a git checkout)"


def harness_version() -> str:
    """Harness package version, plus git SHA when running from a checkout."""
    sha = ""
    try:
        out = subprocess.run(
            ["git", "-C", str(Path(__file__).resolve().parent), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode == 0:
            sha = " @ " + out.stdout.strip()[:12]
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return f"pgbench-harness {__version__}{sha}"


def host_info() -> str:
    """Load-generator host info: uname, CPU count, memory."""
    uname = platform.uname()
    lines = [
        f"uname: {' '.join(uname)}",
        f"cpu_count: {os.cpu_count()}",
    ]
    meminfo = Path("/proc/meminfo")
    if meminfo.exists():
        for line in meminfo.read_text().splitlines():
            if line.startswith(("MemTotal", "MemAvailable", "SwapTotal")):
                lines.append(line.strip())
    return "\n".join(lines) + "\n"


def connection_ceiling_probe(
    spec: Spec, password: str, count: int, logger: logging.Logger
) -> ProbeResult:
    """Open *count* simultaneous connections (cheap ``SELECT pg_sleep`` holders).

    Connections are launched in order with a tiny stagger. We wait the *full*
    grace period (a single fast refusal must not short-circuit the wait, or
    slow-to-establish holders would be miscounted), then classify each holder:
    a process still alive at the deadline is holding an established session
    (success); a process that already exited was refused. The lowest exited
    launch index approximates the connection count at which the target
    refused, and its verbatim (redacted) stderr is captured.
    """
    logger.info("preflight: connection-ceiling probe with %d simultaneous connections", count)
    env = child_env(spec, password)
    hold_sql = engines.hold_sql(spec.target.engine, 30)
    procs: list[subprocess.Popen[str]] = []
    try:
        for _ in range(count):
            procs.append(subprocess.Popen(
                _psql_argv(spec, hold_sql),
                env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
            ))
            time.sleep(0.01)
        grace = float(os.environ.get("PGB_PROBE_GRACE_S", str(PROBE_CONNECT_GRACE_S)))
        time.sleep(grace)  # full wait — established holders sleep for 30s, refusals exit fast
        result = ProbeResult(requested=count, succeeded=0)
        for idx, p in enumerate(procs, start=1):
            if p.poll() is None:
                result.succeeded += 1  # still holding the connection open
            elif result.first_failed_index is None:
                result.first_failed_index = idx
                # Safe to read: an exited process won't block the pipe.
                stderr = p.stderr.read().strip() if p.stderr else ""
                result.first_error = get_redactor().redact(stderr)
        return result
    finally:
        for p in procs:
            if p.poll() is None:
                p.terminate()
        for p in procs:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()   # reap: an unwaited kill leaves a zombie for the
            finally:       # life of a possibly week-long harness process
                if p.stderr and not p.stderr.closed:
                    p.stderr.close()  # avoid leaked-pipe ResourceWarnings


TPCC_TABLE_BASES = (
    "warehouse", "district", "customer", "history", "orders",
    "new_orders", "order_line", "stock", "item",
)
OLTP_SIZE_TOLERANCE = 0.10  # oltp row counts may drift slightly under R/W load


def expected_table_names(spec: Spec) -> list[str]:
    """The exact benchmark tables the configured workload owns."""
    w = spec.workload
    if w.type == "tpcc":
        return [f"{base}{i}" for i in range(1, w.tables + 1) for base in TPCC_TABLE_BASES]
    return [f"sbtest{i}" for i in range(1, w.tables + 1)]


def _count_query(spec: Spec, password: str, sql: str) -> Optional[int]:
    ok, out = psql_query_soft(spec, password, sql)
    return int(out) if ok and out.lstrip("-").isdigit() else None


def _list_query(spec: Spec, password: str, sql: str) -> list[str]:
    ok, out = psql_query_soft(spec, password, sql)
    return [line for line in out.splitlines() if line.strip()] if ok else []


def _sql_str(value: str) -> str:
    """Quote a Python string as a SQL string literal (single quotes doubled)."""
    return "'" + value.replace("'", "''") + "'"


def count_resolvable_tables(spec: Spec, password: str, names: list[str]) -> Optional[int]:
    """Count how many unqualified table names resolve for the session.

    PostgreSQL: ``to_regclass`` so the answer matches exactly how sysbench
    (which issues unqualified ``CREATE``/``SELECT``) resolves the same names,
    across whatever search_path the session has. MySQL has no search path —
    unqualified names resolve in the connection's default schema — so the
    equivalent check is information_schema.tables scoped to ``database()``.
    """
    if spec.target.engine == "mysql":
        in_list = ", ".join(_sql_str(n) for n in names)
        return _count_query(
            spec, password,
            "SELECT count(*) FROM information_schema.tables "
            f"WHERE table_schema = database() AND table_name IN ({in_list})",
        )
    values = ", ".join(f"({_sql_str(n)})" for n in names)
    return _count_query(
        spec, password,
        f"SELECT count(*) FROM (VALUES {values}) v(n) WHERE to_regclass(v.n) IS NOT NULL",
    )


def find_tables_any_schema(spec: Spec, password: str, names: list[str]) -> list[str]:
    """Locate the given table names across *all* schemas, as schema.table strings."""
    in_list = ", ".join(_sql_str(n) for n in names)
    if spec.target.engine == "mysql":
        return _list_query(
            spec, password,
            "SELECT concat(table_schema, '.', table_name) "
            "FROM information_schema.tables "
            f"WHERE table_name IN ({in_list}) "
            "AND table_schema NOT IN ('mysql','information_schema',"
            "'performance_schema','sys') ORDER BY 1",
        )
    return _list_query(
        spec, password,
        f"SELECT schemaname || '.' || tablename FROM pg_catalog.pg_tables "
        f"WHERE tablename IN ({in_list}) ORDER BY 1",
    )


def database_size_bytes(spec: Spec, password: str) -> Optional[int]:
    """Current size of the target database in bytes (best effort)."""
    return _count_query(
        spec, password, engines.database_size_sql(spec.target.engine))


def _check_canary_schema(spec: Spec, password: str) -> tuple[bool, str]:
    """Verify the canary table has the columns the workload's schema defines.

    PostgreSQL resolves the canary via ``to_regclass`` (search_path-aware, so
    it inspects the same table sysbench will use, in whichever schema it
    lives); MySQL checks information_schema.columns in the default schema.
    """
    cols: tuple[str, ...]
    if spec.workload.type == "tpcc":
        table, cols = "warehouse1", ("w_id", "w_ytd")
    else:
        table, cols = "sbtest1", ("id", "k", "c", "pad")
    quoted = ", ".join(_sql_str(c) for c in cols)
    if spec.target.engine == "mysql":
        n = _count_query(
            spec, password,
            "SELECT count(*) FROM information_schema.columns "
            f"WHERE table_schema = database() AND table_name = {_sql_str(table)} "
            f"AND column_name IN ({quoted})",
        )
    else:
        n = _count_query(
            spec, password,
            f"SELECT count(*) FROM pg_catalog.pg_attribute "
            f"WHERE attrelid = to_regclass({_sql_str(table)}) "
            f"AND attname IN ({quoted}) AND attnum > 0 AND NOT attisdropped",
        )
    if n != len(cols):
        return False, (
            f"table '{table}' exists but does not have the expected {spec.workload.type} "
            f"columns ({', '.join(cols)}) — it was probably not created by this tool"
        )
    return True, ""


def _check_dataset_size(spec: Spec, password: str, chk: DatasetCheck) -> None:
    """Compare loaded dataset size against the spec; sets mismatch status."""
    w = spec.workload
    if w.type == "tpcc":
        chk.expected_size = w.scale
        chk.size_unit = "warehouses per table-set"
        chk.actual_size = _count_query(
            spec, password, "SELECT count(*) FROM warehouse1")
        bad = chk.actual_size != w.scale
    else:
        chk.expected_size = w.table_size
        chk.size_unit = "rows in sbtest1"
        chk.actual_size = _count_query(spec, password, "SELECT max(id) FROM sbtest1")
        bad = (
            chk.actual_size is None
            or abs(chk.actual_size - w.table_size) > w.table_size * OLTP_SIZE_TOLERANCE
        )
    if bad:
        chk.status = "mismatch"
        chk.detail = (
            f"dataset size does not match the spec: found {chk.actual_size} "
            f"{chk.size_unit}, but the spec configures {chk.expected_size}. "
            "The harness never reloads or tops up on top of an existing dataset."
        )


def check_dataset(spec: Spec, password: str) -> DatasetCheck:
    """Verify the dataset is present AND conforms to the spec's configured size.

    Checks, in order: every expected benchmark table exists (by name, so
    unrelated tables can't satisfy the check), the canary table has the
    expected schema, and the loaded size matches `scale`/`table_size`
    (tpcc: exact warehouse count; oltp: ±10% on max(id), since R/W runs may
    drift row ids slightly). Also counts foreign (non-benchmark) tables so
    callers can warn about shared databases.
    """
    names = expected_table_names(spec)
    quoted = ", ".join(_sql_str(n) for n in names)
    chk = DatasetCheck(expected_tables=len(names))
    if spec.target.engine == "mysql":
        # MySQL has no search_path: unqualified names resolve in the default
        # schema, which is what the "where does sysbench look" question means.
        chk.search_path = (psql_query_soft(spec, password, "SELECT database()")[1]
                           or "").strip()
    else:
        chk.search_path = (psql_query_soft(spec, password, "SHOW search_path")[1] or "").strip()
    present = count_resolvable_tables(spec, password, names)
    if present is None:
        chk.detail = "could not resolve benchmark tables (connectivity/permissions?)"
        return chk
    chk.present_tables = present
    if spec.target.engine == "mysql":
        chk.foreign_tables = _count_query(
            spec, password,
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema = database() AND table_type = 'BASE TABLE' "
            f"AND table_name NOT IN ({quoted})",
        ) or 0
    else:
        chk.foreign_tables = _count_query(
            spec, password,
            f"SELECT count(*) FROM pg_catalog.pg_tables "
            f"WHERE schemaname NOT IN ('pg_catalog','information_schema') "
            f"AND tablename NOT IN ({quoted})",
        ) or 0
    if present == 0:
        # Not on the search_path — but did sysbench create them in another
        # schema? If so this is a routing/search_path problem, not a load failure.
        chk.found_elsewhere = find_tables_any_schema(spec, password, names)
        if chk.found_elsewhere:
            chk.status = "wrong_schema"
            chk.detail = (
                f"sysbench created the benchmark tables, but they are not on the "
                f"connection's search_path ({chk.search_path or 'unknown'}). Found: "
                + ", ".join(chk.found_elsewhere[:6])
                + (" …" if len(chk.found_elsewhere) > 6 else "")
            )
        else:
            chk.status = "missing"
            chk.detail = (
                f"none of the {len(names)} expected benchmark tables exist in any "
                "schema — the load did not create them"
            )
        return chk
    if present < len(names):
        chk.status = "incomplete"
        chk.detail = (
            f"only {present} of {len(names)} expected benchmark tables resolve on the "
            "search_path — a previous load was interrupted, or the spec's `tables` changed"
        )
        return chk
    schema_ok, schema_err = _check_canary_schema(spec, password)
    if not schema_ok:
        chk.status = "incomplete"
        chk.detail = schema_err
        return chk
    chk.status = "ok"
    _check_dataset_size(spec, password, chk)
    if chk.ok:
        chk.detail = (
            f"all {len(names)} benchmark tables present; "
            f"{chk.actual_size} {chk.size_unit} (matches spec)"
        )
    return chk


def detect_pooler(spec: Spec, password: str) -> str:
    """Best-effort pooler detection; records raw behavior, never fails preflight.

    `SHOW pool_mode` is a PgBouncer admin-console command and is expected to
    fail against both PgBouncer app databases and plain PostgreSQL — the
    verbatim response is recorded as metadata either way. MySQL has no
    PgBouncer analogue the harness can probe safely, so it records n/a.
    """
    if spec.target.engine == "mysql":
        return "n/a (PgBouncer probe is PostgreSQL-only; ProxySQL is transparent here)"
    ok, out = psql_query_soft(spec, password, "SHOW pool_mode")
    if ok:
        return f"pool_mode={out} (pooler admin interface answered)"
    return f"SHOW pool_mode failed (expected against PgBouncer app DBs / plain PG): {out}"


def detect_pg_stat_monitor(spec: Spec, password: str) -> bool:
    """True when the pg_stat_monitor extension is installed (PostgreSQL only)."""
    if spec.target.engine == "mysql":
        return False
    ok, out = psql_query_soft(
        spec, password,
        "SELECT count(*) FROM pg_extension WHERE extname='pg_stat_monitor'",
    )
    return ok and out.strip() == "1"


# ── database administration (create / drop / recreate for prepare) ───────────
MAINTENANCE_DBS = ("defaultdb", "postgres", "template1")   # engine-dispatched below


def _ident(name: str) -> str:
    """Quote a SQL identifier (PostgreSQL spelling; use _ident_for for engines)."""
    return '"' + name.replace('"', '""') + '"'


def _ident_for(spec: Spec, name: str) -> str:
    return engines.quote_ident(spec.target.engine, name)


def _lit(value: str) -> str:
    """Quote a SQL string literal."""
    return "'" + value.replace("'", "''") + "'"


def _psql_on(spec: Spec, sql: str, dbname: str) -> list[str]:
    t = spec.target
    return engines.client_argv(t.engine, t.host, t.port, t.user, dbname,
                               sql, sslmode=t.sslmode)


def psql_on(spec: Spec, password: str, dbname: str, sql: str,
            timeout: int = PSQL_TIMEOUT_S) -> tuple[bool, str]:
    """Run one statement against a *specific* database (for admin ops). (ok, out|err)."""
    try:
        proc = subprocess.run(_psql_on(spec, sql, dbname), env=child_env(spec, password),
                              capture_output=True, text=True, timeout=timeout)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    if proc.returncode != 0:
        return False, get_redactor().redact(proc.stderr.strip())
    return True, proc.stdout.strip()


def maintenance_db(spec: Spec, password: str) -> Optional[str]:
    """First reachable admin database (for CREATE/DROP DATABASE), or None."""
    for db in engines.maintenance_dbs(spec.target.engine):
        if db == spec.target.database:
            continue
        ok, _ = psql_on(spec, password, db, "SELECT 1")
        if ok:
            return db
    return None


def database_exists(spec: Spec, password: str, maint_db: str) -> bool:
    if spec.target.engine == "mysql":
        ok, out = psql_on(spec, password, maint_db,
                          "SELECT 1 FROM information_schema.schemata "
                          f"WHERE schema_name = {_lit(spec.target.database)}")
    else:
        ok, out = psql_on(spec, password, maint_db,
                          f"SELECT 1 FROM pg_database WHERE datname = {_lit(spec.target.database)}")
    return ok and out.strip() == "1"


def create_database(spec: Spec, password: str, maint_db: str) -> tuple[bool, str]:
    return psql_on(spec, password, maint_db,
                   f"CREATE DATABASE {_ident_for(spec, spec.target.database)}")


def drop_database(spec: Spec, password: str, maint_db: str) -> tuple[bool, str]:
    """Terminate other backends on the target DB (PG), then DROP DATABASE IF EXISTS.

    MySQL drops in-place: DROP DATABASE waits on metadata locks itself and
    there is no managed-safe per-database backend terminate."""
    if spec.target.engine != "mysql":
        psql_on(spec, password, maint_db,
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                f"WHERE datname = {_lit(spec.target.database)} AND pid <> pg_backend_pid()")
    return psql_on(spec, password, maint_db,
                   f"DROP DATABASE IF EXISTS {_ident_for(spec, spec.target.database)}")


def drop_benchmark_tables(spec: Spec, password: str) -> tuple[bool, str]:
    """Drop just the workload's benchmark tables (leaves other objects intact)."""
    names = expected_table_names(spec)
    if not names:
        return True, "no benchmark tables to drop"
    cascade = "" if spec.target.engine == "mysql" else " CASCADE"
    stmt = ("DROP TABLE IF EXISTS "
            + ", ".join(_ident_for(spec, n) for n in names) + cascade)
    return psql_query_soft(spec, password, stmt)


def wait_for_db(spec: Spec, password: str, attempts: int = 8, delay: float = 1.5) -> bool:
    """Poll the target DB until it accepts a trivial query (post create/drop flakiness)."""
    for _ in range(attempts):
        ok, _ = psql_query_soft(spec, password, "SELECT 1")
        if ok:
            return True
        time.sleep(delay)
    return False


def enable_pg_stat_monitor(spec: Spec, password: str) -> tuple[bool, str]:
    """Try to enable the pg_stat_monitor extension. Returns (ok, detail).

    ``CREATE EXTENSION IF NOT EXISTS`` is idempotent. pg_stat_monitor must be in
    ``shared_preload_libraries`` (set on the managed cluster / via a restart) for
    the CREATE to succeed; otherwise this returns the server's (redacted) reason.
    PostgreSQL-only; MySQL per-statement stats come from performance_schema.
    """
    if spec.target.engine == "mysql":
        return False, "n/a on MySQL (performance_schema provides statement stats)"
    ok, out = psql_query_soft(
        spec, password, "CREATE EXTENSION IF NOT EXISTS pg_stat_monitor")
    return (ok, "extension enabled" if ok else out)


def snapshot_bgwriter(spec: Spec, password: str) -> str:
    """One-row JSON snapshot of pg_stat_bgwriter (column-set agnostic; PG only)."""
    if spec.target.engine == "mysql":
        return "{}"
    ok, out = psql_query_soft(
        spec, password, "SELECT row_to_json(t) FROM pg_stat_bgwriter t")
    return out if ok else f'{{"error": "{out[:200]}"}}'


def snapshot_io_stats(spec: Spec, password: str) -> dict[str, Any]:
    """Engine-side I/O counters.

    PostgreSQL: pg_stat_io (PG16+), pg_stat_database, pg_stat_wal. MySQL:
    InnoDB buffer-pool / redo counters from performance_schema.global_status,
    mapped into the same three slots (io = physical page reads/writes,
    db = logical vs physical page requests, wal = redo bytes). Each source
    degrades to ``None`` if absent; deltas of two snapshots that bracket a
    level give read/write operation counts for that level.
    """
    out: dict[str, Any] = {"io": None, "db": None, "wal": None}
    if spec.target.engine == "mysql":
        ok, val = psql_query_soft(
            spec, password,
            "SELECT JSON_OBJECT("
            "'reads', (SELECT variable_value FROM performance_schema.global_status "
            "WHERE variable_name='Innodb_data_reads'), "
            "'writes', (SELECT variable_value FROM performance_schema.global_status "
            "WHERE variable_name='Innodb_data_writes'), "
            "'fsyncs', (SELECT variable_value FROM performance_schema.global_status "
            "WHERE variable_name='Innodb_data_fsyncs'))")
        if ok and val:
            out["io"] = _loads(val)
        ok, val = psql_query_soft(
            spec, password,
            "SELECT JSON_OBJECT("
            "'blks_read', (SELECT variable_value FROM performance_schema.global_status "
            "WHERE variable_name='Innodb_buffer_pool_reads'), "
            "'blks_hit', (SELECT variable_value FROM performance_schema.global_status "
            "WHERE variable_name='Innodb_buffer_pool_read_requests'))")
        if ok and val:
            out["db"] = _loads(val)
        ok, val = psql_query_soft(
            spec, password,
            "SELECT JSON_OBJECT("
            "'wal_bytes', (SELECT variable_value FROM performance_schema.global_status "
            "WHERE variable_name='Innodb_os_log_written'))")
        if ok and val:
            out["wal"] = _loads(val)
        return out
    ok, val = psql_query_soft(
        spec, password,
        "SELECT json_build_object("
        "'reads', coalesce(sum(reads),0), 'writes', coalesce(sum(writes),0), "
        "'extends', coalesce(sum(extends),0), 'fsyncs', coalesce(sum(fsyncs),0), "
        "'hits', coalesce(sum(hits),0)) FROM pg_stat_io")
    if ok and val:
        out["io"] = _loads(val)
    ok, val = psql_query_soft(
        spec, password,
        "SELECT json_build_object('blks_read', blks_read, 'blks_hit', blks_hit) "
        "FROM pg_stat_database WHERE datname = current_database()")
    if ok and val:
        out["db"] = _loads(val)
    ok, val = psql_query_soft(
        spec, password,
        "SELECT json_build_object('wal_records', wal_records, 'wal_bytes', wal_bytes, "
        "'wal_fpi', wal_fpi) FROM pg_stat_wal")
    if ok and val:
        out["wal"] = _loads(val)
    return out


def _loads(text: str) -> Optional[dict[str, Any]]:
    import json

    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except ValueError:
        return None


def snapshot_pg_stat_monitor(spec: Spec, password: str, limit: int = 50) -> str:
    """Top statements by total execution time as JSON rows (best effort).

    PostgreSQL: pg_stat_monitor aggregated by queryid (it buckets stats over
    rolling windows, so a query can appear once per bucket). MySQL: the
    performance_schema statement digest summary, mapped onto the same field
    names (timer columns are picoseconds, hence /1e9 for ms).
    """
    if spec.target.engine == "mysql":
        sql = (
            "SELECT coalesce(JSON_ARRAYAGG(JSON_OBJECT("
            "'queryid', t.digest, 'calls', t.calls, "
            "'total_exec_time', t.total_ms, 'mean_exec_time', t.mean_ms, "
            "'rows', t.rows_sent)), '[]') FROM ("
            "SELECT digest, count_star AS calls, "
            "round(sum_timer_wait/1e9, 3) AS total_ms, "
            "round(avg_timer_wait/1e9, 3) AS mean_ms, sum_rows_sent AS rows_sent "
            "FROM performance_schema.events_statements_summary_by_digest "
            f"ORDER BY sum_timer_wait DESC LIMIT {limit}) t"
        )
        ok, out = psql_query_soft(spec, password, sql)
        return out if ok else "[]"
    sql = (
        "SELECT coalesce(json_agg(t), '[]'::json) FROM ("
        "SELECT queryid, sum(calls) AS calls, sum(total_exec_time) AS total_exec_time, "
        "avg(mean_exec_time) AS mean_exec_time, sum(rows) AS rows "
        f"FROM pg_stat_monitor GROUP BY queryid ORDER BY total_exec_time DESC LIMIT {limit}) t"
    )
    ok, out = psql_query_soft(spec, password, sql)
    return out if ok else "[]"


def capture_pg_settings(spec: Spec, password: str) -> str:
    """Full server-settings dump as CSV text (name,setting,unit,source).

    PostgreSQL streams pg_settings via COPY CSV; MySQL dumps
    performance_schema.global_variables into the same four-column shape
    (unit/source have no MySQL analogue and stay blank).
    """
    if spec.target.engine == "mysql":
        out = psql_query(
            spec, password,
            "SELECT variable_name, variable_value "
            "FROM performance_schema.global_variables ORDER BY variable_name",
            timeout=60,
        )
        sep = engines.cell_sep("mysql")
        lines = ["name,setting,unit,source"]
        for row in out.splitlines():
            cells = row.split(sep)
            name = cells[0] if cells else ""
            val = cells[1] if len(cells) > 1 else ""
            q = '"' + val.replace('"', '""') + '"' if any(c in val for c in ',"\n') else val
            lines.append(f"{name},{q},,")
        return "\n".join(lines)
    return psql_query(
        spec, password,
        "COPY (SELECT name, setting, unit, source FROM pg_settings ORDER BY name) "
        "TO STDOUT WITH CSV HEADER",
        timeout=60,
    )


def capture_env(run_dir: Path, spec: Spec, password: str, pf: PreflightResult) -> None:
    """Write the env/ capture directory (settings, versions, host info)."""
    env_dir = run_dir / "env"
    env_dir.mkdir(parents=True, exist_ok=True)
    # All writes go through atomic_write_text, which redacts the registered
    # secret — so even psql output that echoed connection params stays safe.
    if spec.capture.pg_settings:
        atomic_write_text(env_dir / "pg_settings.csv", capture_pg_settings(spec, password) + "\n")
    atomic_write_text(env_dir / "server_version.txt", pf.server_version_full + "\n")
    atomic_write_text(env_dir / "sysbench_version.txt", pf.sysbench_version + "\n")
    atomic_write_text(env_dir / "tpcc_git_sha.txt", pf.tpcc_git_sha + "\n")
    atomic_write_text(env_dir / "harness_git_sha.txt", harness_version() + "\n")
    atomic_write_text(env_dir / "host_info.txt", host_info())


def run_preflight(spec: Spec, password: str, logger: logging.Logger) -> PreflightResult:
    """Run all preflight checks; raise PreflightError on any hard failure."""
    pf = PreflightResult()
    pf.sysbench_version = sysbench_version()
    pf.psql_version = client_version(spec)
    pf.tpcc_git_sha = tpcc_git_sha(spec.workload.tpcc_path)
    logger.info("preflight: %s | %s | tpcc %s", pf.sysbench_version, pf.psql_version, pf.tpcc_git_sha)
    if spec.workload.type == "tpcc" and not Path(spec.workload.tpcc_path, "tpcc.lua").exists():
        raise PreflightError(
            f"tpcc.lua not found in workload.tpcc_path '{spec.workload.tpcc_path}'",
            hint="git clone https://github.com/Percona-Lab/sysbench-tpcc to that path.",
        )
    pf.server_version_full = psql_query(spec, password, "SELECT version()")
    pf.server_version = psql_query(
        spec, password, engines.server_version_sql(spec.target.engine))
    pf.max_connections = psql_query(
        spec, password, engines.max_connections_sql(spec.target.engine))
    pf.pooler_probe = detect_pooler(spec, password)
    logger.info("preflight: server %s, max_connections=%s", pf.server_version, pf.max_connections)
    logger.info("preflight: pooler probe: %s", pf.pooler_probe)
    _check_pg_stat_monitor(spec, password, pf)
    _check_ceiling(spec, password, pf, logger)
    pf.dataset = check_dataset(spec, password)
    logger.info("preflight: dataset [%s]: %s", pf.dataset.status, pf.dataset.detail)
    _collect_warnings(spec, pf)
    for w in pf.warnings:
        logger.warning("preflight: %s", w)
    return pf


def preflight_steps(spec: Spec, password: str,
                    logger: logging.Logger) -> "Iterator[dict[str, str]]":
    """Yield one structured event per preflight check, in order, for a live UI.

    Reuses exactly the same check functions as ``run_preflight`` (the source of
    truth for the run/soak path) — this only changes *packaging* into per-step
    events with a pass/warn/fail status, so the web tier can render a live
    checklist. Each event is ``{"name","status","detail"}`` with status in
    ok|warn|fail|info. A connectivity/tools/dataset failure ends the sequence.
    """
    def ev(name: str, status: str, detail: str) -> dict[str, str]:
        return {"name": name, "status": status, "detail": detail}

    try:
        detail = f"{sysbench_version()} · {client_version(spec)}"
        yield ev("Load-gen tools", "ok", detail)
    except Exception as exc:  # noqa: BLE001
        yield ev("Load-gen tools", "fail", str(exc))
        return
    if spec.workload.type == "tpcc":
        ok = Path(spec.workload.tpcc_path, "tpcc.lua").exists()
        yield ev("sysbench-tpcc scripts", "ok" if ok else "fail",
                 f"tpcc @ {tpcc_git_sha(spec.workload.tpcc_path)}" if ok
                 else f"tpcc.lua not found in {spec.workload.tpcc_path}")
        if not ok:
            return
    try:
        full = psql_query(spec, password, "SELECT version()")
        ver = psql_query(spec, password, engines.server_version_sql(spec.target.engine))
        yield ev("Connectivity", "ok", (full.splitlines()[0][:90] if full else "connected"))
        yield ev("Server version", "ok", ver)
    except Exception as exc:  # noqa: BLE001  (emit a checklist event, never crash the stream)
        yield ev("Connectivity", "fail", str(exc))
        return
    try:
        mx = psql_query(spec, password, engines.max_connections_sql(spec.target.engine))
        peak = peak_threads(spec)
        ok = mx.isdigit() and int(mx) > peak
        yield ev("max_connections", "ok" if ok else "warn",
                 f"{mx} (run peaks at {peak} threads)")
    except Exception as exc:  # noqa: BLE001
        yield ev("max_connections", "warn", str(exc))
    try:
        yield ev("Pooler probe", "info", detect_pooler(spec, password))
    except Exception as exc:  # noqa: BLE001
        yield ev("Pooler probe", "info", str(exc))
    if spec.target.engine == "mysql":
        yield ev("pg_stat_monitor", "info",
                 "n/a on MySQL — per-statement stats come from "
                 "performance_schema statement digests")
    else:
        yield from _pg_stat_monitor_steps(spec, password, ev)
    try:
        peak = peak_threads(spec)
        probe = connection_ceiling_probe(spec, password, peak, logger)
        if probe.succeeded >= probe.requested:
            yield ev("Connection ceiling", "ok",
                     f"{probe.succeeded}/{probe.requested} simultaneous connections OK")
        else:
            yield ev("Connection ceiling", "warn",
                     f"only {probe.succeeded}/{probe.requested} established; first refusal "
                     f"at #{probe.first_failed_index}: {probe.first_error}")
    except Exception as exc:  # noqa: BLE001
        yield ev("Connection ceiling", "warn", str(exc))
    try:
        d = check_dataset(spec, password)
        status = "ok" if d.ok else ("warn" if d.status == "missing" else "fail")
        yield ev("Dataset", status, f"[{d.status}] {d.detail}")
    except Exception as exc:  # noqa: BLE001
        yield ev("Dataset", "fail", str(exc))


def _pg_stat_monitor_steps(spec: Spec, password: str,
                           ev: Any) -> "Iterator[dict[str, str]]":
    """The PostgreSQL-only pg_stat_monitor preflight step (extracted verbatim)."""
    try:
        # Snapshotting the view is read-only and harmless — but the PRELOAD
        # LIBRARY is not: pg_stat_monitor showed sustained memory growth that
        # took down multi-hour tests (field report 2026-07-22). Surface a
        # warning whenever it is loaded, independent of capture settings.
        spl_advisory = ""
        try:
            spl = psql_query(spec, password, "SHOW shared_preload_libraries")
            if "pg_stat_monitor" in (spl or ""):
                spl_advisory = (" NOTE: pg_stat_monitor is in "
                                "shared_preload_libraries — known memory-growth "
                                "issue under sustained load (verified 2026-07 on "
                                "long TPC-C runs); prefer pg_stat_statements "
                                "for multi-hour tests.")
        except Exception:  # noqa: BLE001 — advisory only
            pass
        if detect_pg_stat_monitor(spec, password):
            yield ev("pg_stat_monitor", "warn" if spl_advisory else "ok",
                     "enabled (per-query latency/calls captured)." + spl_advisory)
        else:
            ok, detail = enable_pg_stat_monitor(spec, password)
            if ok and detect_pg_stat_monitor(spec, password):
                yield ev("pg_stat_monitor", "warn" if spl_advisory else "ok",
                         "was disabled — enabled it now." + spl_advisory)
            else:
                yield ev("pg_stat_monitor", "info",
                         f"not enabled ({detail}) — per-query capture will "
                         "come from pg_stat_statements if available")
    except Exception as exc:  # noqa: BLE001
        yield ev("pg_stat_monitor", "warn", str(exc))


def peak_threads(spec: Spec) -> int:
    """Highest concurrency a run will reach (any mode), for preflight sizing."""
    if spec.soak is not None:
        return spec.soak.threads
    if spec.continuous is not None:
        return spec.continuous.threads
    if spec.suite is not None:
        return max(spec.suite.threads)
    assert spec.sweep is not None
    return max(spec.sweep.threads)


def _collect_warnings(spec: Spec, pf: PreflightResult) -> None:
    """Non-fatal conditions worth surfacing in logs, manifest and report."""
    cpus = os.cpu_count() or 1
    max_threads = peak_threads(spec)
    if max_threads > cpus * 8:
        pf.warnings.append(
            f"the run peaks at {max_threads} client threads but this load generator "
            f"has only {cpus} CPUs — the loadgen itself may become the bottleneck "
            "at high thread counts (check host_info.txt when interpreting results)"
        )
    if pf.dataset and pf.dataset.foreign_tables:
        pf.warnings.append(
            f"{pf.dataset.foreign_tables} non-benchmark table(s) exist in schema "
            "'public' — this database is shared, so cache/disk contention from "
            "that data may pollute results; a dedicated database is recommended"
        )


def _check_pg_stat_monitor(spec: Spec, password: str, pf: PreflightResult) -> None:
    mode = spec.capture.pg_stat_monitor
    if mode == "false":
        return
    if spec.target.engine == "mysql":
        if mode == "true":
            raise PreflightError(
                "capture.pg_stat_monitor is true but the target engine is MySQL "
                "(pg_stat_monitor is a PostgreSQL extension)",
                hint="set capture.pg_stat_monitor to auto/false for MySQL targets; "
                     "per-statement stats come from performance_schema.",
            )
        return
    pf.pg_stat_monitor = detect_pg_stat_monitor(spec, password)
    if mode == "true" and not pf.pg_stat_monitor:
        raise PreflightError(
            "capture.pg_stat_monitor is true but the extension is not installed",
            hint="CREATE EXTENSION pg_stat_monitor (it must be in shared_preload_libraries), "
                 "or set capture.pg_stat_monitor to auto/false.",
        )


def _check_ceiling(
    spec: Spec, password: str, pf: PreflightResult, logger: logging.Logger
) -> None:
    count = peak_threads(spec)
    pf.probe = connection_ceiling_probe(spec, password, count, logger)
    if not pf.probe.ok:
        raise PreflightError(
            f"connection-ceiling probe failed: only {pf.probe.succeeded} of "
            f"{pf.probe.requested} simultaneous connections succeeded; connection "
            f"#{pf.probe.first_failed_index} was refused with:\n  {pf.probe.first_error}",
            hint=(
                "the target (likely its pooler, e.g. PgBouncer max_client_conn) refuses "
                f"{count} clients. Raise the pooler/client limit or trim sweep.threads "
                "below the ceiling before launching a long sweep."
            ),
        )
    logger.info(
        "preflight: connection ceiling OK (%d/%d simultaneous connections)",
        pf.probe.succeeded, pf.probe.requested,
    )


# ── live PostgreSQL metrics sampler (engine-side; runs during run/soak) ──────
#
# A lightweight background sampler that records engine-side metrics every few
# seconds during a run, so the web cockpit (and the report) can show what the
# database was doing — cache-hit %, WAL throughput, active connections, server
# transactions/s — alongside the load-generator timeline. This is the same
# honest caveat as the IOPS proxy: these are PostgreSQL's own counters (no shell
# on the managed host). It reuses psql_query_soft and never raises — a failed
# sample is skipped, never breaking the run. The password is the run's password
# (already injected by the worker / read from the spec env var); only numbers are
# ever written to disk, so nothing secret can land in the timeseries.

_PG_MB = 1024 * 1024
# One round-trip per sample: a flat JSON row of cumulative counters + gauges,
# all engine-side (pg_stat_* views) so it works on managed PG; only numbers ever
# reach disk. Counter ALIASES are stable across PG versions even though the
# checkpoint source moved from pg_stat_bgwriter to pg_stat_checkpointer in PG17,
# so pg_delta_row stays version-agnostic.
_DB_SUMS = ("xact_commit", "xact_rollback", "blks_read", "blks_hit",
            "tup_returned", "tup_fetched", "tup_inserted", "tup_updated", "tup_deleted",
            "deadlocks", "conflicts", "temp_bytes", "temp_files")
_DB_SELECT = ", ".join(f"COALESCE(sum({c}),0) AS {c}" for c in _DB_SUMS)
_CKPT_PG17 = ("COALESCE(num_timed,0) AS ckpt_timed, COALESCE(num_requested,0) AS ckpt_req, "
              "COALESCE(write_time,0) AS ckpt_write_ms, COALESCE(sync_time,0) AS ckpt_sync_ms "
              "FROM pg_stat_checkpointer")
_CKPT_PRE17 = ("COALESCE(checkpoints_timed,0) AS ckpt_timed, COALESCE(checkpoints_req,0) AS ckpt_req, "
               "COALESCE(checkpoint_write_time,0) AS ckpt_write_ms, "
               "COALESCE(checkpoint_sync_time,0) AS ckpt_sync_ms FROM pg_stat_bgwriter")
_WAL_SRC = "SELECT COALESCE(wal_bytes,0) AS wal_bytes FROM pg_stat_wal"
_WAL_NONE = "SELECT 0 AS wal_bytes"


def _build_live_sql(ckpt: str, wal_src: str) -> str:
    return (
        "SELECT row_to_json(t) FROM (SELECT "
        # Server-side read time: rate denominators use the delta between two
        # clock_timestamp()s (the true counter-sampling interval), not the harness's
        # round-trip timing, so a slow sample under load can't skew the rate.
        "EXTRACT(EPOCH FROM clock_timestamp()) AS db_epoch, "
        # Cluster-wide saturation gauges (max_connections is cluster-scoped, so the
        # connection counts are intentionally NOT filtered to a single database).
        "(SELECT count(*) FROM pg_stat_activity WHERE state='active') AS active, "
        "(SELECT count(*) FROM pg_stat_activity) AS total_conn, "
        # Lock contention on the TARGET database: backends blocked waiting on a lock,
        # and the longest such wait (proxy: age of the blocked query). This is the
        # engine-side cause behind sysbench's err/s (deadlock/serialization retries).
        "(SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() "
        "AND wait_event_type='Lock' AND state='active') AS blocked, "
        "(SELECT COALESCE(EXTRACT(EPOCH FROM max(clock_timestamp()-query_start)),0) "
        "FROM pg_stat_activity WHERE datname=current_database() "
        "AND wait_event_type='Lock' AND state='active') AS lock_wait_max_s, "
        "d.*, (d.xact_commit + d.xact_rollback) AS xacts, "
        "w.wal_bytes, c.ckpt_timed, c.ckpt_req, c.ckpt_write_ms, c.ckpt_sync_ms, "
        "b.bgw_clean, b.bgw_alloc, "
        "(SELECT EXTRACT(EPOCH FROM max(replay_lag)) FROM pg_stat_replication) AS repl_replay_lag "
        # pg_stat_database is scoped to the TARGET database (the sampler connects to
        # it, so current_database() is the benchmark DB). Summing every database
        # inflated the workload counters with other DBs / shared-catalog activity —
        # per-database tools like PMM show one DB, which is the discrepancy this fixes.
        f"FROM (SELECT {_DB_SELECT} FROM pg_stat_database WHERE datname=current_database()) d, "
        f"({wal_src}) w, (SELECT {ckpt}) c, "
        "(SELECT COALESCE(buffers_clean,0) AS bgw_clean, COALESCE(buffers_alloc,0) AS bgw_alloc "
        "FROM pg_stat_bgwriter) b) t"
    )


# Try newest-first; the first variant whose views all exist wins (the PG18 target
# uses pg_stat_checkpointer; older servers fall back to pg_stat_bgwriter / no WAL view).
LIVE_PG_SQLS = (
    _build_live_sql(_CKPT_PG17, _WAL_SRC),
    _build_live_sql(_CKPT_PRE17, _WAL_SRC),
    _build_live_sql(_CKPT_PG17, _WAL_NONE),
    _build_live_sql(_CKPT_PRE17, _WAL_NONE),
)

# MySQL: one round trip over performance_schema.global_status, pulled as a
# single JSON document (JSON_OBJECTAGG: MySQL 5.7+/MariaDB 10.5+). The raw
# counters are mapped in _mysql_live_row onto the SAME key names the PG
# sampler uses, so pg_delta_row / LIVE_PG_COLUMNS / the cockpit charts work
# unchanged: blks_hit/blks_read become buffer-pool hit/miss page requests
# (so cache_hit_pct is the buffer-pool hit ratio), wal_bytes is redo bytes
# written, xact_* are Com_commit/Com_rollback. Gauges with no MySQL
# equivalent are simply absent and chart as gaps.
_MYSQL_STATUS_VARS = (
    "Com_commit", "Com_rollback",
    "Innodb_buffer_pool_read_requests", "Innodb_buffer_pool_reads",
    "Innodb_os_log_written", "Threads_connected", "Threads_running",
    "Innodb_rows_read", "Innodb_rows_inserted", "Innodb_rows_updated",
    "Innodb_rows_deleted", "Innodb_row_lock_current_waits",
    "Created_tmp_disk_tables", "Created_tmp_tables",
)
LIVE_MYSQL_SQL = (
    "SELECT JSON_OBJECT('db_epoch', UNIX_TIMESTAMP(NOW(4)), 'status', "
    "(SELECT JSON_OBJECTAGG(LOWER(variable_name), variable_value) "
    "FROM performance_schema.global_status WHERE variable_name IN ("
    + ", ".join("'" + v + "'" for v in _MYSQL_STATUS_VARS)
    + ")))"
)


def _mysql_live_row(doc: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Map the raw MySQL status JSON onto the PG sampler's key vocabulary."""
    status = doc.get("status")
    if not isinstance(status, dict):
        return None

    def n(key: str) -> Optional[float]:
        v = status.get(key.lower())
        if v is None:
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    requests, reads = n("Innodb_buffer_pool_read_requests"), n("Innodb_buffer_pool_reads")
    row: dict[str, Any] = {
        "db_epoch": doc.get("db_epoch"),
        "active": n("Threads_running") or 0,
        "total_conn": n("Threads_connected") or 0,
        "blocked": n("Innodb_row_lock_current_waits") or 0,
        "xact_commit": n("Com_commit"),
        "xact_rollback": n("Com_rollback"),
        "wal_bytes": n("Innodb_os_log_written"),
        "tup_returned": n("Innodb_rows_read"),
        "tup_inserted": n("Innodb_rows_inserted"),
        "tup_updated": n("Innodb_rows_updated"),
        "tup_deleted": n("Innodb_rows_deleted"),
        "temp_files": n("Created_tmp_disk_tables"),
    }
    if requests is not None and reads is not None:
        row["blks_read"] = reads
        row["blks_hit"] = max(0.0, requests - reads)
    if row["xact_commit"] is not None and row["xact_rollback"] is not None:
        row["xacts"] = row["xact_commit"] + row["xact_rollback"]
    return row
LIVE_PG_COLUMNS = (
    "t", "active", "total_conn", "blocked_queries", "lock_wait_max_s",
    "xacts_s", "commits_s", "rollbacks_s",
    "cache_hit_pct", "blks_read_s", "blks_hit_s", "wal_mb_s",
    "tup_returned_s", "tup_fetched_s", "tup_inserted_s", "tup_updated_s", "tup_deleted_s",
    "deadlocks_s", "conflicts_s", "temp_bytes_s", "temp_files_s",
    "ckpt_timed_s", "ckpt_req_s", "ckpt_write_ms_s", "ckpt_sync_ms_s",
    "bgw_clean_s", "bgw_alloc_s", "repl_replay_lag_s",
)
# Live samples use a short timeout: a stuck sample (e.g. during a failover) must
# not block the sampler for 30s or leave an orphan psql after the run ends.
LIVE_PG_TIMEOUT_S = 8


def live_pg_query(spec: Spec, password: str) -> Optional[dict[str, Any]]:
    """One engine-side sample (cumulative counters + gauges); None if unavailable.

    PostgreSQL tries variants newest-first (PG17 checkpointer, then pre-17
    bgwriter; with then without pg_stat_wal), so the richest query a server
    supports wins. MySQL pulls the global_status subset in one round trip and
    maps it onto the same keys. Never raises; a short timeout means a stalled
    sample is skipped, not blocking.
    """
    if spec.target.engine == "mysql":
        ok, out = psql_query_soft(spec, password, LIVE_MYSQL_SQL,
                                  timeout=LIVE_PG_TIMEOUT_S)
        if ok and out.strip():
            try:
                doc = json.loads(out.strip().splitlines()[0])
                if isinstance(doc, dict):
                    return _mysql_live_row(doc)
            except (ValueError, IndexError):
                return None
        return None
    for sql in LIVE_PG_SQLS:
        ok, out = psql_query_soft(spec, password, sql, timeout=LIVE_PG_TIMEOUT_S)
        if ok and out.strip():
            try:
                row = json.loads(out.strip().splitlines()[0])
                if isinstance(row, dict):
                    return dict(row)
            except (ValueError, IndexError):
                continue
    return None


def _pg_rate(prev: dict[str, Any], cur: dict[str, Any], key: str, dt: float,
             scale: float = 1.0) -> Any:
    """Per-second rate of a cumulative counter, RESET-AWARE: a counter that went
    backwards (server restart / failover to a new primary / pg_stat_reset) yields
    "" (a chart gap), never a misleading 0 or a huge first-delta spike — which is
    the WAL ~15,000 MB/s artifact this fixes."""
    a, b = prev.get(key), cur.get(key)
    if a is None or b is None or a == "" or b == "":
        return ""
    try:
        a, b = float(a), float(b)
    except (TypeError, ValueError):
        return ""
    if b < a:
        return ""
    return round((b - a) / scale / dt, 3)


def _pg_gauge(cur: dict[str, Any], key: str) -> Any:
    v = cur.get(key)
    if v is None or v == "":
        return ""
    try:
        return round(float(v), 3)
    except (TypeError, ValueError):
        return ""


def _sample_dt(prev: dict[str, Any], cur: dict[str, Any]) -> float:
    """Seconds between the two counter snapshots. Prefer the server clock
    (clock_timestamp at read time) so the rate denominator is the true sampling
    interval; fall back to the harness monotonic clock if db_epoch is unavailable."""
    try:
        a, b = cur.get("db_epoch"), prev.get("db_epoch")
        if a is not None and b is not None:
            d = float(a) - float(b)
            if d > 0:
                return d
    except (TypeError, ValueError):
        pass
    return max(1e-9, float(cur["_mono"]) - float(prev["_mono"]))


def pg_delta_row(prev: dict[str, Any], cur: dict[str, Any], t: float) -> dict[str, Any]:
    """Build one timeseries row from two cumulative samples (rates over the gap).

    Cache-hit % is computed *over the interval* (delta hits / delta accesses), so
    it reflects current cache behaviour — the key re-warm signal after a storage
    reattach — not the since-startup average. All rates are reset-aware.
    """
    dt = _sample_dt(prev, cur)
    d_hit = float(cur.get("blks_hit", 0)) - float(prev.get("blks_hit", 0))
    d_read = float(cur.get("blks_read", 0)) - float(prev.get("blks_read", 0))
    accesses = d_hit + d_read
    hit_pct = (round(100.0 * d_hit / accesses, 2)
               if accesses > 0 and d_hit >= 0 and d_read >= 0 else "")
    return {
        "t": int(round(t)),
        "active": int(cur.get("active", 0) or 0),
        "total_conn": int(cur.get("total_conn", 0) or 0),
        "blocked_queries": int(float(cur.get("blocked", 0) or 0)),
        "lock_wait_max_s": _pg_gauge(cur, "lock_wait_max_s"),
        "xacts_s": _pg_rate(prev, cur, "xacts", dt),
        "commits_s": _pg_rate(prev, cur, "xact_commit", dt),
        "rollbacks_s": _pg_rate(prev, cur, "xact_rollback", dt),
        "cache_hit_pct": hit_pct,
        "blks_read_s": _pg_rate(prev, cur, "blks_read", dt),
        "blks_hit_s": _pg_rate(prev, cur, "blks_hit", dt),
        "wal_mb_s": _pg_rate(prev, cur, "wal_bytes", dt, scale=_PG_MB),
        "tup_returned_s": _pg_rate(prev, cur, "tup_returned", dt),
        "tup_fetched_s": _pg_rate(prev, cur, "tup_fetched", dt),
        "tup_inserted_s": _pg_rate(prev, cur, "tup_inserted", dt),
        "tup_updated_s": _pg_rate(prev, cur, "tup_updated", dt),
        "tup_deleted_s": _pg_rate(prev, cur, "tup_deleted", dt),
        "deadlocks_s": _pg_rate(prev, cur, "deadlocks", dt),
        "conflicts_s": _pg_rate(prev, cur, "conflicts", dt),
        "temp_bytes_s": _pg_rate(prev, cur, "temp_bytes", dt),
        "temp_files_s": _pg_rate(prev, cur, "temp_files", dt),
        "ckpt_timed_s": _pg_rate(prev, cur, "ckpt_timed", dt),
        "ckpt_req_s": _pg_rate(prev, cur, "ckpt_req", dt),
        "ckpt_write_ms_s": _pg_rate(prev, cur, "ckpt_write_ms", dt),
        "ckpt_sync_ms_s": _pg_rate(prev, cur, "ckpt_sync_ms", dt),
        "bgw_clean_s": _pg_rate(prev, cur, "bgw_clean", dt),
        "bgw_alloc_s": _pg_rate(prev, cur, "bgw_alloc", dt),
        "repl_replay_lag_s": _pg_gauge(cur, "repl_replay_lag"),
    }


class LivePgSampler:
    """Background thread sampling engine-side PG metrics into parsed/pg_timeseries.csv.

    Best-effort: it owns no part of the run's correctness. Construct with the
    run's spec/password/run_dir, ``start()`` before the load and ``stop()`` after.
    """

    def __init__(self, spec: Spec, password: str, run_dir: Path,
                 interval_s: int = 5, logger: Optional[logging.Logger] = None) -> None:
        self.spec = spec
        self.password = password
        self.path = run_dir / "parsed" / "pg_timeseries.csv"
        self.interval = max(1, int(interval_s))
        self.logger = logger
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _sample(self) -> Optional[dict[str, Any]]:
        s = live_pg_query(self.spec, self.password)
        if s is not None:
            s["_mono"] = time.monotonic()
        return s

    def _append(self, row: dict[str, Any]) -> None:
        try:
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(",".join(str(row[c]) for c in LIVE_PG_COLUMNS) + "\n")
        except OSError:
            pass

    def _run(self) -> None:
        # every iteration is guarded: one unexpected error (transient outage,
        # odd server answer) must never kill the sampler thread for the rest
        # of a week-long run — it skips the sample and keeps going
        t0 = time.monotonic()
        errors = 0
        try:
            prev = self._sample()
        except Exception:  # noqa: BLE001
            prev = None
        while not self._stop.wait(self.interval):
            try:
                cur = self._sample()
                if cur is None:
                    continue
                if prev is not None:
                    self._append(pg_delta_row(prev, cur, cur["_mono"] - t0))
                prev = cur
            except Exception as exc:  # noqa: BLE001
                errors += 1
                if self.logger and errors <= 3:
                    self.logger.warning("live PG sampler: sample skipped (%s)%s",
                                        exc, " — further errors suppressed"
                                        if errors == 3 else "")
                prev = None

    def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(self.path, "w", encoding="utf-8") as fh:
                fh.write(",".join(LIVE_PG_COLUMNS) + "\n")
        except OSError:
            return
        self._thread = threading.Thread(target=self._run, name="pg-sampler", daemon=True)
        self._thread.start()
        if self.logger:
            self.logger.info("live PG sampler started (every %ds) -> %s",
                             self.interval, self.path.name)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval + 5)
