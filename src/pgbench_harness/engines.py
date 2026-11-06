"""Database-engine abstraction: PostgreSQL vs MySQL command/dialect plumbing.

The harness drives every engine through the same three channels — sysbench's
native driver, the engine's own CLI client for SQL, and environment variables
for the credential — so one module owns the differences:

* **Secrets policy is engine-independent**: the password goes to child
  processes only via environment (``PGPASSWORD`` for libpq, ``MYSQL_PWD`` for
  libmysqlclient — both honored by the respective client AND by sysbench's
  driver when no password option is given), never on a command line. argv is
  therefore always safe to log, for either engine.
* SQL dialect snippets used by probes/preflight live here so callers never
  branch on engine themselves.
"""

from __future__ import annotations

import os
from typing import Any, Optional

ENGINES = ("postgres", "mysql")
DEFAULT_ENGINE = "postgres"
DEFAULT_PORTS = {"postgres": 5432, "mysql": 3306}

# sslmode is spelled in PostgreSQL vocabulary in specs/targets for both
# engines (one UI, one spec schema); it maps onto the MySQL client's
# --ssl-mode values here.
_MYSQL_SSL_MODE = {
    "disable": "DISABLED",
    "allow": "PREFERRED",
    "prefer": "PREFERRED",
    "require": "REQUIRED",
    "verify-ca": "VERIFY_CA",
    "verify-full": "VERIFY_IDENTITY",
}


def normalize_engine(value: Optional[str]) -> str:
    """Canonical engine name; '' / None mean the PostgreSQL default."""
    v = (value or DEFAULT_ENGINE).strip().lower()
    if v in ("pg", "postgresql"):
        v = "postgres"
    return v


def client_binary(engine: str) -> str:
    return "mysql" if engine == "mysql" else "psql"


def client_install_hint(engine: str) -> str:
    if engine == "mysql":
        return "apt-get install mysql-client (or default-mysql-client)."
    return "apt-get install postgresql-client (see README)."


def client_argv(engine: str, host: str, port: int, user: str, database: str,
                sql: str, *, sslmode: str = "require",
                connect_timeout_s: Optional[int] = None,
                statement_timeout_ms: Optional[int] = None) -> list[str]:
    """One-shot SQL invocation argv for the engine's CLI client.

    Output contract (shared by both engines): no headers, no column names,
    rows on stdout one per line, cells separated by :func:`cell_sep`.
    No secret ever appears here — passwords travel via :func:`client_env`.
    """
    if engine == "mysql":
        argv = ["mysql", "-h", str(host), "-P", str(port), "-u", str(user),
                "--batch", "--skip-column-names",
                f"--ssl-mode={_MYSQL_SSL_MODE.get(sslmode, 'REQUIRED')}"]
        if connect_timeout_s is not None:
            argv.append(f"--connect-timeout={int(connect_timeout_s)}")
        if statement_timeout_ms is not None:
            # caps SELECTs server-side (the read probe / collector queries);
            # the subprocess timeout remains the hard stop for everything else
            argv.append("--init-command=SET SESSION "
                        f"max_execution_time={int(statement_timeout_ms)}")
        if database:
            argv += ["-D", str(database)]
        argv += ["-e", sql]
        return argv
    # psql: timeouts are passed via env (PGCONNECT_TIMEOUT / PGOPTIONS) by
    # client_env so this argv matches the one the harness always used.
    return ["psql", "-h", str(host), "-p", str(port), "-U", str(user),
            "-d", str(database), "-X", "-A", "-t", "-v", "ON_ERROR_STOP=1",
            "-c", sql]


def client_env(engine: str, password: str, sslmode: str = "require",
               connect_timeout_s: Optional[int] = None,
               statement_timeout_ms: Optional[int] = None) -> dict[str, str]:
    """Child-process environment: credential (and, for PG, ssl/timeouts).

    MySQL's ssl mode and timeouts ride in argv (non-secret flags); the
    password is ``MYSQL_PWD``, which libmysqlclient reads whenever no
    explicit password option is supplied — the same no-secret-in-argv
    contract as libpq's ``PGPASSWORD``.

    When TLS is on, the standard OpenSSL variables are pointed at the
    system CA bundle: sysbench's MySQL driver (and some client builds)
    initialize their TLS context via ``SSL_CTX_set_default_verify_paths``,
    which fails outright on stock Ubuntu (no default cert *file*, only the
    certs dir) with ``error 2026: SSL_CTX_set_default_verify_paths
    failed``. sysbench 1.0.20 has no ``--mysql-ssl-ca`` option, so the env
    route is the one that works everywhere. Peer verification is NOT
    enabled by this — it only lets the context initialize.
    """
    env = dict(os.environ)
    if engine == "mysql":
        env["MYSQL_PWD"] = password
        if sslmode != "disable":
            ca = mysql_ssl_ca()
            if ca:
                env.setdefault("SSL_CERT_FILE", ca)
                env.setdefault("SSL_CERT_DIR", os.path.dirname(ca) or "/etc/ssl/certs")
        return env
    env["PGPASSWORD"] = password
    env["PGSSLMODE"] = sslmode
    if connect_timeout_s is not None:
        env["PGCONNECT_TIMEOUT"] = str(int(connect_timeout_s))
    if statement_timeout_ms is not None:
        env["PGOPTIONS"] = f"-c statement_timeout={int(statement_timeout_ms)}"
    return env


def cell_sep(engine: str) -> str:
    """Column separator in the client's row output (psql -A: '|'; mysql --batch: TAB)."""
    return "\t" if engine == "mysql" else "|"


_MYSQL_CA_BUNDLES = (
    "/etc/ssl/certs/ca-certificates.crt",   # Debian/Ubuntu
    "/etc/pki/tls/certs/ca-bundle.crt",     # RHEL/Fedora
    "/etc/ssl/cert.pem",                    # Alpine/macOS
)


def mysql_ssl_ca() -> str:
    """CA bundle the OpenSSL env vars point at when MySQL TLS is on (see
    client_env). ``PGB_MYSQL_SSL_CA`` overrides the autodetected path — set
    it to a provider CA file if you want one, or to the empty string to
    leave the environment untouched."""
    override = os.environ.get("PGB_MYSQL_SSL_CA")
    if override is not None:
        return override
    for p in _MYSQL_CA_BUNDLES:
        if os.path.exists(p):
            return p
    return ""


def sysbench_connection_args(engine: str, host: str, port: int, user: str,
                             database: str, sslmode: str = "require") -> list[str]:
    """sysbench driver + connection flags. Password stays OUT of argv for both
    drivers (PGPASSWORD / MYSQL_PWD via the child env). TLS trust setup also
    rides the env (client_env), never argv: sysbench 1.0.20 — the Ubuntu
    package — rejects --mysql-ssl-ca as an invalid option."""
    if engine == "mysql":
        ssl = "off" if sslmode == "disable" else "on"
        return [
            "--db-driver=mysql",
            f"--mysql-host={host}",
            f"--mysql-port={port}",
            f"--mysql-user={user}",
            f"--mysql-db={database}",
            f"--mysql-ssl={ssl}",
        ]
    return [
        "--db-driver=pgsql",
        f"--pgsql-host={host}",
        f"--pgsql-port={port}",
        f"--pgsql-user={user}",
        f"--pgsql-db={database}",
    ]


# ── SQL dialect snippets ─────────────────────────────────────────────

def hold_sql(engine: str, seconds: int = 30) -> str:
    """A query that holds its connection open (the connection-ceiling probe)."""
    if engine == "mysql":
        return f"SELECT sleep({int(seconds)})"
    return f"SELECT pg_sleep({int(seconds)})"


def canary_write_sql(engine: str) -> str:
    """Create-if-absent + single-row upsert on the harness canary table."""
    if engine == "mysql":
        return (
            "CREATE TABLE IF NOT EXISTS pgbench_harness_canary "
            "(id INT PRIMARY KEY, ts TIMESTAMP(6) NULL); "
            "INSERT INTO pgbench_harness_canary (id, ts) VALUES (1, NOW(6)) "
            "ON DUPLICATE KEY UPDATE ts = VALUES(ts)")
    return (
        "CREATE TABLE IF NOT EXISTS pgbench_harness_canary "
        "(id int PRIMARY KEY, ts timestamptz); "
        "INSERT INTO pgbench_harness_canary (id, ts) VALUES (1, now()) "
        "ON CONFLICT (id) DO UPDATE SET ts = excluded.ts")


def server_version_sql(engine: str) -> str:
    return "SELECT @@version" if engine == "mysql" else "SHOW server_version"


def max_connections_sql(engine: str) -> str:
    return "SELECT @@max_connections" if engine == "mysql" else "SHOW max_connections"


def quote_ident(engine: str, name: str) -> str:
    if engine == "mysql":
        return "`" + name.replace("`", "``") + "`"
    return '"' + name.replace('"', '""') + '"'


def maintenance_dbs(engine: str) -> tuple[str, ...]:
    """Admin databases to try for CREATE/DROP DATABASE."""
    if engine == "mysql":
        return ("mysql", "information_schema")
    return ("defaultdb", "postgres", "template1")


def database_size_sql(engine: str) -> str:
    if engine == "mysql":
        return ("SELECT coalesce(sum(data_length + index_length), 0) "
                "FROM information_schema.tables WHERE table_schema = database()")
    return "SELECT pg_database_size(current_database())"


def spec_target_engine(target: Any) -> str:
    """Engine of a Spec.target / plain dict target, defaulting to postgres."""
    if isinstance(target, dict):
        return normalize_engine(str(target.get("engine") or ""))
    return normalize_engine(getattr(target, "engine", ""))
