"""Worker-side Continuous Mode machinery: desired-state reconciliation and the
relaunch loop that makes an always-on workload survive reboots and crashes.

The contract (see OPERATIONS.md → Continuous mode):

* ``jobs.desired_state`` ('running' | 'stopped') is the durable USER intent for
  ``kind='continuous'`` jobs. The actual job state converges toward it.
* A continuous job whose harness process is gone while desired_state='running'
  is NOT failed — it is re-queued, and the worker relaunches the harness with
  ``--run-dir <same run>`` so a new segment appends to the same run directory.
  This is the droplet-reboot survival path (reconcile_startup) and the
  harness-crash survival path (the worker housekeeping tick).
* An explicit user stop sets desired_state='stopped' BEFORE signalling, so the
  intent survives even if every process dies mid-stop.

Ingest, probing, collection, and alert-engine loops also live behind
``housekeeping``/thread entrypoints in this module (later phases of the same
feature) so the worker loop only ever calls two functions.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from pgbench_webapp import alerts, contmetrics, queries
from pgbench_webapp.config import Config
from pgbench_webapp.util import utc_now_iso

# A crashed harness is relaunched no sooner than this after it died, so a
# crash-looping binary cannot hot-loop the queue (the supervisor's own sysbench
# backoff handles load-generator churn; this guards the harness process itself).
RELAUNCH_HOLDOFF_S = 30


def get_int_setting(conn: sqlite3.Connection, key: str, default: int) -> int:
    try:
        return int(queries.get_setting(conn, key, str(default)) or default)
    except (TypeError, ValueError):
        return default


def continuous_cap(conn: sqlite3.Connection) -> int:
    return max(1, get_int_setting(conn, "continuous_cap", 4))


def _append_run_event(results_dir: Path, run_id: Optional[str], etype: str,
                      label: str, note: str) -> None:
    """Append one event to the run's events.jsonl (same shape the harness
    writes). Best-effort: a missing/deleted run dir must not block reconcile."""
    if not run_id:
        return
    run_dir = results_dir / run_id
    if not run_dir.is_dir():
        return
    ev = {"ts_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
          "type": etype, "label": label, "note": note, "source": "auto"}
    try:
        with open(run_dir / "events.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(ev) + "\n")
    except OSError:
        pass


def requeue_continuous(cfg: Config, conn: sqlite3.Connection, job: sqlite3.Row,
                       reason: str) -> None:
    """Re-queue a continuous job whose harness process is gone but whose
    desired_state is 'running': the single relaunch primitive shared by the
    startup reconcile (reboot) and the housekeeping tick (harness crash).

    Records a ``loadgen_restart`` event in the run's events.jsonl and an
    informational ``harness_relaunch`` alert row.
    """
    queries.update_job(conn, job["id"], state="queued", pid=None, pid_start="",
                       exit_code=None, error="", finished_utc=None)
    _append_run_event(cfg.results_dir, job["run_id"], "loadgen_restart",
                      "harness relaunch", reason)
    try:
        # Rate-limit the informational ledger row: a crash-looping harness is
        # relaunched every holdoff tick forever ("never give up"), which must
        # not mint thousands of alert rows a day.
        recent = conn.execute(
            "SELECT fired_utc FROM alerts WHERE job_id=? AND type='harness_relaunch' "
            "ORDER BY id DESC LIMIT 1", (job["id"],)).fetchone()
        rate_limited = False
        if recent is not None:
            try:
                last = datetime.strptime(recent["fired_utc"], "%Y-%m-%dT%H:%M:%SZ") \
                    .replace(tzinfo=timezone.utc).timestamp()
                rate_limited = (datetime.now(timezone.utc).timestamp() - last) < 600
            except (ValueError, TypeError):
                rate_limited = False
        if not rate_limited:
            alerts.store_alert(conn, type="harness_relaunch", severity="info",
                               dedup_key=f"job:{job['id']}:harness_relaunch",
                               job_id=job["id"], point=True,
                               context={"reason": reason, "run_id": job["run_id"]})
    except sqlite3.Error:
        pass                     # the relaunch must proceed even if the ledger hiccups
    queries.audit(conn, None, "continuous_relaunch",
                  target=job["run_id"] or f"job:{job['id']}", detail=reason)


def reconcile_dead_continuous(cfg: Config, conn: sqlite3.Connection,
                              job: sqlite3.Row) -> None:
    """Startup reconcile for a continuous job whose pid is dead.

    desired_state='running'  -> relaunch (the reboot-survival case).
    anything else            -> converge to canceled (an explicit stop, or a
                                legacy row with no recorded intent).
    """
    desired = (job["desired_state"] if "desired_state" in job.keys() else "") or ""
    if job["state"] != "canceling" and desired == "running":
        requeue_continuous(cfg, conn, job,
                           "worker startup: harness process gone with "
                           "desired_state=running (reboot or crash)")
        return
    queries.update_job(conn, job["id"], state="canceled", pid=None,
                       finished_utc=utc_now_iso(),
                       error="stopped (converged at worker startup)")


def _requeue_crashed(cfg: Config, conn: sqlite3.Connection) -> None:
    """Housekeeping: relaunch continuous jobs that ended while their
    desired_state is still 'running' (harness crash mid-life). The holdoff
    keeps a crash-looping harness from hot-looping the queue."""
    holdoff = max(5, get_int_setting(conn, "cont_relaunch_holdoff_s",
                                     RELAUNCH_HOLDOFF_S))
    cutoff = datetime.now(timezone.utc).timestamp() - holdoff
    for job in conn.execute(
            "SELECT * FROM jobs WHERE kind='continuous' AND desired_state='running' "
            "AND state IN ('failed', 'canceled', 'done')"):
        fin = job["finished_utc"] or ""
        try:
            fin_ts = datetime.strptime(fin, "%Y-%m-%dT%H:%M:%SZ") \
                .replace(tzinfo=timezone.utc).timestamp()
        except (ValueError, TypeError):
            fin_ts = 0.0
        if fin_ts > cutoff:
            continue             # died too recently — wait out the holdoff
        requeue_continuous(cfg, conn, job,
                           f"harness process ended (state={job['state']}) with "
                           "desired_state=running — relaunching")


def housekeeping(cfg: Config, conn: sqlite3.Connection) -> None:
    """Periodic worker tick (~every 30s): converge continuous jobs toward
    their desired state, apply retention, keep the WAL bounded, and pulse the
    dead-man heartbeat. Never raises."""
    try:
        _requeue_crashed(cfg, conn)
    except Exception:  # noqa: BLE001 — housekeeping must never kill the worker loop
        pass
    try:
        contmetrics.prune(cfg, conn)
    except Exception:  # noqa: BLE001
        pass
    try:
        contmetrics.maybe_wal_checkpoint(conn)
    except Exception:  # noqa: BLE001
        pass
    try:
        dead_man_heartbeat(cfg, conn)
    except Exception:  # noqa: BLE001
        pass


# ── dead-man heartbeat ──────────────────────────────────────────────
# If the droplet powers off, nothing here can send an alert — so the check is
# INVERTED: while >=1 continuous workload is desired-running, GET the
# operator's healthchecks.io-style URL every ~60s; the external service alerts
# when the pings STOP. Failures are logged, never alerted (the monitor of the
# monitor must not recurse). Dormant when the setting is empty.

_last_beat = 0.0
HEARTBEAT_INTERVAL_S = 60.0


def dead_man_heartbeat(cfg: Config, conn: sqlite3.Connection) -> bool:
    global _last_beat
    if time.monotonic() - _last_beat < HEARTBEAT_INTERVAL_S:
        return False
    url = (queries.get_setting(conn, "heartbeat_url", "") or "").strip()
    if not url or not url.startswith(("http://", "https://")):
        return False
    row = conn.execute("SELECT 1 FROM jobs WHERE kind='continuous' "
                       "AND desired_state='running' LIMIT 1").fetchone()
    if row is None:
        return False
    _last_beat = time.monotonic()
    import urllib.request
    try:
        urllib.request.urlopen(url, timeout=5).close()  # nosec - operator-supplied URL
        return True
    except Exception as exc:  # noqa: BLE001 — log, never alert, never raise
        import logging
        logging.getLogger("pgbench-webapp").warning(
            "dead-man heartbeat GET failed: %s", exc)
        return False


# ── the alert engine: sample/collector/supervisor-derived conditions ─

ENGINE_TICK_S = 10.0


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _set_condition(conn: sqlite3.Connection, job_id: Optional[int], atype: str,
                   severity: str, firing: bool, context: dict[str, Any],
                   suppressed: bool = False) -> None:
    key = (f"job:{job_id}:{atype}" if job_id is not None else f"global:{atype}")
    if firing:
        alerts.store_alert(conn, type=atype, severity=severity, dedup_key=key,
                           job_id=job_id, context=context, suppressed=suppressed)
    else:
        alerts.resolve_alert(conn, key)


def evaluate_job_conditions(cfg: Config, conn: sqlite3.Connection,
                            job: sqlite3.Row, acfg: dict[str, Any],
                            now: Optional[datetime] = None) -> None:
    """Sample-derived + supervisor-derived alert rules for one running job."""
    from pgbench_harness.continuous import read_state
    from pgbench_webapp import contprobe
    jid = int(job["id"])
    now = now or datetime.now(timezone.utc)
    now_iso = _iso(now)
    suppressed = contprobe.in_maintenance(conn, jid, now_iso)

    # error_rate (warn): err/s above threshold for >= hold seconds
    hold = int(acfg["err_rate_hold_s"])
    thr = float(acfg["err_rate_threshold"])
    row = conn.execute(
        "SELECT count(*) AS n, min(err_s) AS mn, avg(err_s) AS av "
        "FROM cont_samples WHERE job_id=? AND ts_utc >= ?",
        (jid, _iso(now - timedelta(seconds=hold)))).fetchone()
    firing = (int(row["n"] or 0) >= max(5, int(hold * 0.8))
              and float(row["mn"] or 0.0) > thr)
    _set_condition(conn, jid, "error_rate", "warn", firing,
                   {"detail": f"err/s > {thr:g} for {hold}s "
                              f"(avg {float(row['av'] or 0):.1f}/s)"}, suppressed)

    # latency (warn): p99 above threshold for >= hold seconds
    lhold = int(acfg["latency_hold_s"])
    lthr = float(acfg["latency_p99_ms"])
    row = conn.execute(
        "SELECT count(*) AS n, min(lat_p99) AS mn, avg(lat_p99) AS av "
        "FROM cont_samples WHERE job_id=? AND ts_utc >= ?",
        (jid, _iso(now - timedelta(seconds=lhold)))).fetchone()
    firing = (int(row["n"] or 0) >= max(5, int(lhold * 0.8))
              and float(row["mn"] or 0.0) > lthr)
    _set_condition(conn, jid, "latency", "warn", firing,
                   {"detail": f"p99 > {lthr:g}ms for {lhold}s "
                              f"(avg {float(row['av'] or 0):.0f}ms)"}, suppressed)

    # tps_drop (warn): 5-min TPS below X% of the trailing 24h median — skipped
    # until ~24h of rollup history exists (a young workload has no baseline).
    day_ago = _iso(now - timedelta(hours=24))
    cnt = int(conn.execute(
        "SELECT count(*) FROM cont_rollup_1m WHERE job_id=? AND ts_utc >= ?",
        (jid, day_ago)).fetchone()[0])
    if cnt >= 1320:                                # >= ~22h of minutes
        med = conn.execute(
            "SELECT tps_avg FROM cont_rollup_1m WHERE job_id=? AND ts_utc >= ? "
            "ORDER BY tps_avg LIMIT 1 OFFSET ?",
            (jid, day_ago, cnt // 2)).fetchone()
        cur = conn.execute(
            "SELECT count(*) AS n, avg(tps) AS av FROM cont_samples "
            "WHERE job_id=? AND ts_utc >= ?",
            (jid, _iso(now - timedelta(minutes=5)))).fetchone()
        median = float(med["tps_avg"] or 0.0) if med else 0.0
        pct = float(acfg["tps_drop_pct"])
        firing = (median > 0 and int(cur["n"] or 0) >= 60
                  and float(cur["av"] or 0.0) < median * pct / 100.0)
        _set_condition(conn, jid, "tps_drop", "warn", firing,
                       {"detail": f"5-min TPS {float(cur['av'] or 0):.0f} < "
                                  f"{pct:g}% of 24h median {median:.0f}"},
                       suppressed)

    # auth_failure (crit): the supervisor classified the last exit as auth
    if job["run_id"]:
        st = read_state(cfg.results_dir / str(job["run_id"]))
        firing = (st.get("last_error_class") == "auth"
                  and st.get("status") in ("backoff", "load_stopped"))
        _set_condition(conn, jid, "auth_failure", "crit", firing,
                       {"detail": str(st.get("last_error") or "")[:300]},
                       suppressed)

    # no_data (crit): neither samples nor probe results for > no_data_s while
    # the job claims to be running — the pipeline itself is wedged.
    nds = float(acfg["no_data_s"])
    started_ok = False
    try:
        started = datetime.strptime(job["started_utc"] or "",
                                    "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        started_ok = (now - started).total_seconds() > nds
    except (ValueError, TypeError):
        pass
    last_sample = contmetrics.last_sample_utc(conn, jid)
    sample_stale = True
    if last_sample:
        try:
            dt = datetime.strptime(last_sample, "%Y-%m-%dT%H:%M:%SZ") \
                .replace(tzinfo=timezone.utc)
            sample_stale = (now - dt).total_seconds() > nds
        except ValueError:
            pass
    probe_tick = contprobe.LAST_PROBE_TICK.get(jid)
    probe_stale = probe_tick is None or (time.monotonic() - probe_tick) > nds
    firing = started_ok and sample_stale and probe_stale
    _set_condition(conn, jid, "no_data", "crit", firing,
                   {"detail": f"no samples and no probe results for > {nds:.0f}s"},
                   suppressed)


def check_loadgen_disk(cfg: Config, conn: sqlite3.Connection,
                       acfg: dict[str, Any]) -> None:
    """loadgen_disk (warn): the SQLite/results volume filling up. Global
    (job_id NULL); 5-point hysteresis so it doesn't flap on the boundary."""
    import shutil
    try:
        du = shutil.disk_usage(cfg.data_dir)
    except OSError:
        return
    pct = du.used / du.total * 100.0 if du.total else 0.0
    warn_at = float(acfg["disk_warn_pct"])
    if pct > warn_at:
        _set_condition(conn, None, "loadgen_disk", "warn", True,
                       {"detail": f"data dir at {pct:.0f}% (> {warn_at:.0f}%)"})
    elif pct < warn_at - 5:
        _set_condition(conn, None, "loadgen_disk", "warn", False, {})


def engine_tick(cfg: Config, conn: sqlite3.Connection, store: Any, *,
                deliver_kwargs: Optional[dict[str, Any]] = None) -> None:
    """One alert-engine pass: evaluate conditions, deliver pending rows,
    re-notify standing crits."""
    from pgbench_webapp import contprobe
    acfg = contprobe.get_alerts_config(conn)
    for job in conn.execute("SELECT * FROM jobs WHERE kind='continuous' "
                            "AND state='running'"):
        try:
            evaluate_job_conditions(cfg, conn, job, acfg)
        except sqlite3.Error:
            continue
    check_loadgen_disk(cfg, conn, acfg)
    alerts.deliver_pending(conn, store, **(deliver_kwargs or {}))
    alerts.renotify_crits(conn, store,
                          renotify_min=int(acfg["renotify_min"]))


def _engine_loop(cfg: Config) -> None:
    from pgbench_webapp.db import connect
    from pgbench_webapp.secrets_store import SecretStore
    conn = connect(cfg.db_path)
    store = SecretStore(cfg.secret_key_path, cfg.data_dir / "secrets.enc")
    while True:
        try:
            engine_tick(cfg, conn, store)
        except Exception:  # noqa: BLE001 — the engine must outlive any one bad tick
            pass
        time.sleep(ENGINE_TICK_S)


# ── background loops (owned by the worker process) ──────────────────

_bg_lock = threading.Lock()
_bg_started = False
INGEST_TICK_S = 3.0


def _active_continuous_jobs(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Continuous jobs whose run dir may still be producing bytes: live ones,
    plus recently-finished ones so the final lines land after a stop."""
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=15)) \
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    return list(conn.execute(
        "SELECT * FROM jobs WHERE kind='continuous' AND run_id IS NOT NULL "
        "AND (state IN ('running', 'canceling') "
        "     OR (finished_utc IS NOT NULL AND finished_utc >= ?))", (cutoff,)))


def _ingest_loop(cfg: Config) -> None:
    """One thread multiplexing every active continuous job's ingest: tail the
    raw segment logs into cont_samples + rollups. Also acts as the prober
    manager: spawns/reaps one prober thread per RUNNING continuous job.
    Owns its own connection; prober threads own theirs."""
    from pgbench_webapp import contprobe
    from pgbench_webapp.db import connect
    conn = connect(cfg.db_path)
    probers: dict[int, tuple[threading.Thread, threading.Event]] = {}
    while True:
        try:
            active_ids: set[int] = set()
            for job in _active_continuous_jobs(conn):
                jid = int(job["id"])
                try:
                    contmetrics.ingest_job(cfg, conn, jid, str(job["run_id"]))
                except sqlite3.Error:
                    pass         # busy/locked: the cursor didn't advance — retried next tick
                if job["state"] == "running":
                    active_ids.add(jid)
            # prober lifecycle: one thread per running continuous job
            for jid in list(probers):
                t, ev = probers[jid]
                if jid not in active_ids or not t.is_alive():
                    if jid not in active_ids:
                        ev.set()
                    if not t.is_alive():
                        probers.pop(jid, None)
            for jid in active_ids:
                if jid not in probers or not probers[jid][0].is_alive():
                    ev = threading.Event()
                    t = threading.Thread(target=contprobe.probe_job_loop,
                                         args=(cfg, jid, ev),
                                         name=f"cont-probe-{jid}", daemon=True)
                    probers[jid] = (t, ev)
                    t.start()
        except Exception:  # noqa: BLE001 — the ingester must outlive any one bad tick
            pass
        time.sleep(INGEST_TICK_S)


def start_background(cfg: Config) -> None:
    """Start the worker-owned continuous-mode threads exactly once."""
    global _bg_started
    with _bg_lock:
        if _bg_started:
            return
        _bg_started = True
    threading.Thread(target=_ingest_loop, args=(cfg,), name="cont-ingest",
                     daemon=True).start()
    threading.Thread(target=_engine_loop, args=(cfg,), name="cont-engine",
                     daemon=True).start()
    from pgbench_webapp.contcollect import _collector_loop
    threading.Thread(target=_collector_loop, args=(cfg,), name="cont-collect",
                     daemon=True).start()
