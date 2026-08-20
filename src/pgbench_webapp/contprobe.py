"""Availability prober + outage ledger for Continuous Mode.

Independent of sysbench: per continuous job a loop probes the target over
psql — a READ probe (``SELECT 1``) and a WRITE probe (a single-row canary
upsert; the table is created if absent and never grows). Each probe kind runs
its own state machine::

    healthy -> degraded (1..M-1 consecutive failures) -> down (>= M)

Entering ``down`` opens a row in the ``outages`` ledger and fires a
``db_unreachable`` crit alert; recovery closes the row with its duration and
fires ``db_recovered``. A third outage kind, ``load``, is opened when the
LOAD's samples stop flowing while both probes stay green — a loadgen-side
problem, deliberately a distinct class so it can never masquerade as a
database outage. Outages overlapping a maintenance window are ``planned`` and
their alerts are stored suppressed (ledger yes, Slack no).

The probes use the same no-psycopg approach as the rest of the repo: psql
with short timeouts, one failing query = one failed probe, never an exception
out of the loop.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import yaml

from pgbench_harness.util import get_redactor
from pgbench_webapp import alerts, queries
from pgbench_webapp.config import Config

# ── alert/prober configuration (the cont_alerts_config setting) ─────

ALERTS_CONFIG_KEY = "cont_alerts_config"

ALERTS_CONFIG_DEFAULTS: dict[str, Any] = {
    "probe_interval_s": 5,          # per-kind probe cadence
    "probe_failures_to_down": 3,    # M: consecutive failures -> down
    "probe_connect_timeout_s": 3,
    "probe_statement_timeout_ms": 3000,
    "load_gap_s": 30,               # sample silence (probes green) -> load outage
    "err_rate_threshold": 1.0,      # err/s sustained ...
    "err_rate_hold_s": 60,          # ... for this long -> warn
    "latency_p99_ms": 500.0,        # p99 sustained ...
    "latency_hold_s": 300,          # ... for this long -> warn
    "tps_drop_pct": 50,             # 5-min TPS below this % of 24h median -> warn
    "renotify_min": 60,             # crit "still firing" cadence
    "disk_warn_pct": 85,            # data-dir usage -> loadgen_disk warn
    "no_data_s": 120,               # no samples AND no probe results -> crit
}


def get_alerts_config(conn: sqlite3.Connection) -> dict[str, Any]:
    """Stored thresholds merged over the defaults (unknown keys tolerated)."""
    out = dict(ALERTS_CONFIG_DEFAULTS)
    raw = queries.get_setting(conn, ALERTS_CONFIG_KEY, "")
    if raw:
        try:
            doc = json.loads(raw)
            if isinstance(doc, dict):
                for k, v in doc.items():
                    if isinstance(v, (int, float)) and not isinstance(v, bool):
                        out[k] = v
        except ValueError:
            pass
    return out


# ── maintenance windows ─────────────────────────────────────────────

def in_maintenance(conn: sqlite3.Connection, job_id: int, at_utc: str) -> bool:
    """True when *at_utc* falls inside a window for this job or a global one."""
    row = conn.execute(
        "SELECT 1 FROM maintenance_windows WHERE (job_id IS NULL OR job_id=?) "
        "AND starts_utc <= ? AND ends_utc >= ? LIMIT 1",
        (job_id, at_utc, at_utc)).fetchone()
    return row is not None


def overlaps_maintenance(conn: sqlite3.Connection, job_id: int,
                         start_utc: str, end_utc: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM maintenance_windows WHERE (job_id IS NULL OR job_id=?) "
        "AND starts_utc <= ? AND ends_utc >= ? LIMIT 1",
        (job_id, end_utc, start_utc)).fetchone()
    return row is not None


# ── the probe state machine + outage ledger ─────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class OutageTracker:
    """healthy -> degraded -> down state machine for ONE (job, kind); owns the
    corresponding rows in the outages ledger and the transition alerts."""

    def __init__(self, conn: sqlite3.Connection, job_id: int, kind: str,
                 failures_to_down: int = 3,
                 log: Optional[Callable[[str], None]] = None) -> None:
        self.conn = conn
        self.job_id = job_id
        self.kind = kind                      # 'read' | 'write' | 'load'
        self.failures_to_down = max(1, failures_to_down)
        self.status = "healthy"               # healthy | degraded | down
        self.consecutive_failures = 0
        self.first_failure_utc = ""
        self.first_error = ""
        self.outage_id: Optional[int] = None
        self._log = log or (lambda _m: None)
        self._adopt_open_outage()

    def _adopt_open_outage(self) -> None:
        """A worker restart mid-outage re-attaches to the open ledger row
        instead of opening a duplicate."""
        row = self.conn.execute(
            "SELECT id, started_utc, first_error FROM outages WHERE job_id=? "
            "AND kind=? AND ended_utc IS NULL ORDER BY id DESC LIMIT 1",
            (self.job_id, self.kind)).fetchone()
        if row is not None:
            self.outage_id = int(row["id"])
            self.status = "down"
            self.consecutive_failures = self.failures_to_down
            self.first_failure_utc = str(row["started_utc"])
            self.first_error = str(row["first_error"] or "")
            self._log(f"{self.kind}: adopted open outage #{self.outage_id}")

    @property
    def dedup_key(self) -> str:
        return f"job:{self.job_id}:db_unreachable:{self.kind}"

    def observe(self, ok: bool, error: str = "",
                now_utc: str = "") -> Optional[str]:
        """Feed one probe result; returns the transition that occurred
        ('degraded' | 'down' | 'recovered') or None."""
        now = now_utc or _now_iso()
        if ok:
            if self.status == "down":
                self._close_outage(now)
                self.status = "healthy"
                self.consecutive_failures = 0
                self.first_failure_utc = self.first_error = ""
                return "recovered"
            prev = self.status
            self.status = "healthy"
            self.consecutive_failures = 0
            self.first_failure_utc = self.first_error = ""
            return None if prev == "healthy" else None
        # failure path
        self.consecutive_failures += 1
        if self.consecutive_failures == 1:
            self.first_failure_utc = now
            self.first_error = (error or "").strip()[:500]
        self._log(f"{self.kind}: probe failure #{self.consecutive_failures} "
                  f"({(error or '')[:120]})")
        if self.consecutive_failures < self.failures_to_down:
            if self.status != "degraded":
                self.status = "degraded"
                return "degraded"
            return None
        if self.status != "down":
            self.status = "down"
            self._open_outage(now, error)
            return "down"
        return None

    def _open_outage(self, now: str, error: str) -> None:
        from pgbench_harness.continuous import classify_error
        started = self.first_failure_utc or now
        planned = in_maintenance(self.conn, self.job_id, started)
        cur = self.conn.execute(
            "INSERT INTO outages(job_id, kind, started_utc, error_class, "
            "first_error, planned) VALUES (?,?,?,?,?,?)",
            (self.job_id, self.kind, started,
             classify_error(self.first_error or error or ""),
             self.first_error, 1 if planned else 0))
        self.outage_id = int(cur.lastrowid or 0)
        atype = "db_unreachable" if self.kind in ("read", "write") else "load_gap"
        sev = "crit" if self.kind in ("read", "write") else "warn"
        alerts.store_alert(
            self.conn, type=atype, severity=sev, dedup_key=self.dedup_key,
            job_id=self.job_id, suppressed=planned,
            context={"kind": self.kind, "started_utc": started,
                     "first_error": self.first_error,
                     "failures": self.consecutive_failures})
        self._log(f"{self.kind}: DOWN — outage #{self.outage_id} opened"
                  + (" (planned/maintenance)" if planned else ""))

    def _close_outage(self, now: str) -> None:
        if self.outage_id is None:
            return
        row = self.conn.execute("SELECT started_utc, planned FROM outages WHERE id=?",
                                (self.outage_id,)).fetchone()
        duration = 0.0
        planned = bool(row and row["planned"])
        if row is not None:
            try:
                t0 = datetime.strptime(str(row["started_utc"]), "%Y-%m-%dT%H:%M:%SZ")
                t1 = datetime.strptime(now, "%Y-%m-%dT%H:%M:%SZ")
                duration = max(0.0, (t1 - t0).total_seconds())
            except ValueError:
                pass
            if not planned and overlaps_maintenance(
                    self.conn, self.job_id, str(row["started_utc"]), now):
                planned = True
        self.conn.execute(
            "UPDATE outages SET ended_utc=?, duration_s=?, planned=? WHERE id=?",
            (now, duration, 1 if planned else 0, self.outage_id))
        alerts.resolve_alert(self.conn, self.dedup_key)
        atype = "db_recovered" if self.kind in ("read", "write") else "load_resumed"
        alerts.store_alert(
            self.conn, type=atype, severity="info",
            dedup_key=f"job:{self.job_id}:{atype}:{self.kind}",
            job_id=self.job_id, point=True, suppressed=planned,
            context={"kind": self.kind, "duration_s": duration,
                     "outage_id": self.outage_id})
        self._log(f"{self.kind}: recovered after {duration:.0f}s "
                  f"(outage #{self.outage_id} closed)")
        self.outage_id = None

    def close_open_outage(self, reason: str) -> None:
        """Job stopped: an open ledger row must not dangle forever."""
        if self.outage_id is not None:
            now = _now_iso()
            self._close_outage(now)
            self.conn.execute(
                "UPDATE outages SET first_error = first_error || ? "
                "WHERE job_id=? AND ended_utc=? AND first_error NOT LIKE ?",
                (f" [{reason}]", self.job_id, now, f"%[{reason}]%"))


# ── probe execution (psql; short timeouts; never raises) ────────────

READ_SQL = "SELECT 1"
WRITE_SQL = (
    "CREATE TABLE IF NOT EXISTS pgbench_harness_canary "
    "(id int PRIMARY KEY, ts timestamptz); "
    "INSERT INTO pgbench_harness_canary (id, ts) VALUES (1, now()) "
    "ON CONFLICT (id) DO UPDATE SET ts = excluded.ts")


def run_probe(target: dict[str, Any], password: str, kind: str,
              connect_timeout_s: int = 3,
              statement_timeout_ms: int = 3000) -> tuple[bool, str]:
    """One probe over psql. Returns (ok, redacted-error). Never raises."""
    sql = READ_SQL if kind == "read" else WRITE_SQL
    argv = ["psql", "-h", str(target.get("host", "")),
            "-p", str(target.get("port", 5432)),
            "-U", str(target.get("user", "")),
            "-d", str(target.get("database", "")),
            "-X", "-A", "-t", "-v", "ON_ERROR_STOP=1", "-c", sql]
    env = dict(os.environ)
    env["PGPASSWORD"] = password
    env["PGSSLMODE"] = str(target.get("sslmode", "require"))
    env["PGCONNECT_TIMEOUT"] = str(connect_timeout_s)
    env["PGOPTIONS"] = f"-c statement_timeout={int(statement_timeout_ms)}"
    try:
        proc = subprocess.run(
            argv, env=env, capture_output=True, text=True,
            timeout=connect_timeout_s + statement_timeout_ms / 1000.0 + 5)
    except subprocess.TimeoutExpired:
        return False, f"probe timed out after {connect_timeout_s}s+ (hard cap)"
    except OSError as exc:
        return False, f"psql could not run: {exc}"
    if proc.returncode != 0:
        err = get_redactor().redact((proc.stderr or proc.stdout or "").strip())
        return False, err.splitlines()[-1][:500] if err else f"exit {proc.returncode}"
    return True, ""


def job_target(conn: sqlite3.Connection, store: Any,
               job: sqlite3.Row) -> Optional[tuple[dict[str, Any], str]]:
    """(connection-info, password) for a continuous job, freshly resolved so a
    rotated credential is picked up without a restart. None if unresolvable."""
    try:
        doc = yaml.safe_load(job["spec_yaml"]) or {}
        target = dict(doc.get("target") or {})
    except yaml.YAMLError:
        return None
    password: Optional[str] = None
    if job["target_id"]:
        tgt = queries.get_target(conn, int(job["target_id"]))
        if tgt is not None:
            target.update(host=tgt["host"], port=tgt["port"],
                          database=tgt["dbname"], user=tgt["dbuser"],
                          sslmode=tgt["sslmode"])
            password = store.get(tgt["password_ref"])
    if not password or not target.get("host"):
        return None
    get_redactor().register(password)
    return target, password


# ── per-job prober loop (thread) ────────────────────────────────────

# last successful-or-failed probe tick per job (monotonic) — the no_data
# alert distinguishes "target down" (probes still reporting) from "the
# worker's own machinery is wedged" (nothing reporting at all).
LAST_PROBE_TICK: dict[int, float] = {}


def _job_active(conn: sqlite3.Connection, job_id: int) -> bool:
    job = queries.get_job(conn, job_id)
    return job is not None and job["state"] in ("running", "canceling")


def probe_job_loop(cfg: Config, job_id: int,
                   stop_event: Optional[threading.Event] = None,
                   max_ticks: int = 0) -> None:
    """The per-job prober: read + write probes each interval, plus the
    load-gap cross-check against the ingested samples. Exits when the job
    stops; closes any outage rows it holds open."""
    from pgbench_webapp import contmetrics
    from pgbench_webapp.db import connect
    from pgbench_webapp.secrets_store import SecretStore
    conn = connect(cfg.db_path)
    store = SecretStore(cfg.secret_key_path, cfg.data_dir / "secrets.enc")
    acfg = get_alerts_config(conn)
    m = int(acfg["probe_failures_to_down"])
    read_t = OutageTracker(conn, job_id, "read", m)
    write_t = OutageTracker(conn, job_id, "write", m)
    load_t = OutageTracker(conn, job_id, "load", 1)
    ticks = 0
    try:
        while not (stop_event and stop_event.is_set()):
            ticks += 1
            if max_ticks and ticks > max_ticks:
                break
            if not _job_active(conn, job_id):
                break
            acfg = get_alerts_config(conn)
            resolved = None
            job = queries.get_job(conn, job_id)
            if job is not None:
                resolved = job_target(conn, store, job)
            if resolved is not None:
                target, pw = resolved
                for kind, tracker in (("read", read_t), ("write", write_t)):
                    ok, err = run_probe(
                        target, pw, kind,
                        connect_timeout_s=int(acfg["probe_connect_timeout_s"]),
                        statement_timeout_ms=int(acfg["probe_statement_timeout_ms"]))
                    tracker.observe(ok, err)
                LAST_PROBE_TICK[job_id] = time.monotonic()
                _check_load_gap(conn, job_id, acfg, read_t, write_t, load_t,
                                contmetrics.last_sample_utc(conn, job_id))
            interval = max(1.0, float(acfg["probe_interval_s"]))
            deadline = time.monotonic() + interval
            while time.monotonic() < deadline:
                if stop_event and stop_event.is_set():
                    return
                time.sleep(0.2)
    except Exception:  # noqa: BLE001 — a prober death must not take the worker down
        pass
    finally:
        for t in (read_t, write_t, load_t):
            try:
                t.close_open_outage("job stopped")
            except sqlite3.Error:
                pass
        LAST_PROBE_TICK.pop(job_id, None)
        conn.close()


def _check_load_gap(conn: sqlite3.Connection, job_id: int, acfg: dict[str, Any],
                    read_t: OutageTracker, write_t: OutageTracker,
                    load_t: OutageTracker, last_sample: str) -> None:
    """Open a kind='load' outage when the LOAD's samples stall while both
    probes are green — a loadgen-side problem, distinct from a DB outage."""
    if not last_sample:
        return                       # never produced a sample: nothing to compare
    try:
        last = datetime.strptime(last_sample, "%Y-%m-%dT%H:%M:%SZ") \
            .replace(tzinfo=timezone.utc)
    except ValueError:
        return
    gap = (datetime.now(timezone.utc) - last).total_seconds()
    probes_green = read_t.status == "healthy" and write_t.status == "healthy"
    if gap > float(acfg["load_gap_s"]) and probes_green:
        load_t.observe(False, f"no samples for {gap:.0f}s while probes are green")
    elif gap <= float(acfg["load_gap_s"]):
        load_t.observe(True)
