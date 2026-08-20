"""Continuous Mode HTTP API: lifecycle (submit/stop/resume), windowed
historical metrics, the outage ledger, alert history, and maintenance windows.

Registered from app.py in the same closure style as ops_routes. RBAC mirrors
the rest of the console: viewers read, operators mutate, admins tune settings
(settings live in app.py's admin settings endpoints).

Submission rule: continuous jobs MUST reference a saved target whose password
lives in the encrypted store. A per-job password is rejected outright — the
per-job secret is deleted when run_job finishes, so a reboot relaunch would
have nothing to authenticate with.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Optional

import yaml
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from pgbench_harness.continuous import read_state
from pgbench_webapp import alerts, contmetrics, harness_api, queries
from pgbench_webapp.config import Config
from pgbench_webapp.secrets_store import SecretStore
from pgbench_webapp.security import CSRF_FIELD

CONT_WORKLOADS = ("tpcc", "oltp_read_only", "oltp_read_write", "oltp_write_only")

_JOB_FIELDS = ("id", "kind", "state", "desired_state", "run_id", "target_id",
               "requested_by", "created_utc", "started_utc", "finished_utc",
               "exit_code", "error")


def _active_clause() -> str:
    """Jobs that hold the one-per-target slot: anything not definitively
    stopped — running/queued states, plus terminal ones the relaunch loop
    will resurrect because their desired_state is still 'running'."""
    return ("kind='continuous' AND (state IN ('queued', 'running', 'canceling') "
            "OR desired_state='running')")


def build_spec_yaml(tgt: sqlite3.Row, payload: dict[str, Any]) -> str:
    """A continuous spec from a saved target + form fields; validated by the
    harness validator before enqueue (the single source of spec rules)."""
    label = str(payload.get("label") or f"continuous-{tgt['name']}").strip()
    wtype = str(payload.get("workload_type") or "oltp_read_write")
    if wtype not in CONT_WORKLOADS:
        raise HTTPException(400, f"workload_type must be one of {CONT_WORKLOADS}")
    try:
        threads = int(payload.get("threads") or 16)
        tables = int(payload.get("tables") or (1 if wtype == "tpcc" else 8))
    except (TypeError, ValueError):
        raise HTTPException(400, "threads/tables must be integers")
    workload: dict[str, Any] = {"type": wtype, "tables": tables}
    if wtype == "tpcc":
        workload["tpcc_path"] = str(payload.get("tpcc_path")
                                    or "/opt/sysbench-tpcc")
        try:
            workload["scale"] = int(payload.get("scale") or 10)
        except (TypeError, ValueError):
            raise HTTPException(400, "scale must be an integer")
    else:
        try:
            workload["table_size"] = int(payload.get("table_size") or 100000)
        except (TypeError, ValueError):
            raise HTTPException(400, "table_size must be an integer")
    continuous: dict[str, Any] = {"threads": threads}
    for opt in ("segment_time_s", "max_consecutive_failures"):
        if payload.get(opt) is not None:
            try:
                continuous[opt] = int(payload[opt])
            except (TypeError, ValueError):
                raise HTTPException(400, f"{opt} must be an integer")
    doc = {
        "run": {"label": label, "edition": "advanced", "tshirt_size": ""},
        "target": {"host": tgt["host"], "port": tgt["port"],
                   "database": tgt["dbname"], "user": tgt["dbuser"],
                   "password_env": "PGB_TARGET_PASSWORD",
                   "sslmode": tgt["sslmode"]},
        "workload": workload,
        "continuous": continuous,
    }
    return yaml.safe_dump(doc, sort_keys=False)


def register(app: FastAPI, cfg: Config, store: SecretStore) -> None:
    from pgbench_webapp.app import (_check_csrf, get_conn, require)
    from pgbench_webapp.worker import stop_job_process

    def _cont_job(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row:
        job = queries.get_job(conn, job_id)
        if job is None or job["kind"] != "continuous":
            raise HTTPException(404, "continuous job not found")
        return job

    def _range(window: str, frm: str, to: str) -> tuple[Any, Any]:
        try:
            return contmetrics.resolve_range(window, frm, to)
        except contmetrics.RangeError as exc:
            raise HTTPException(400, str(exc))

    # ── lifecycle ──
    @app.post("/api/continuous")
    def cont_create(request: Request, payload: dict,
                    conn: sqlite3.Connection = Depends(get_conn),
                    user: sqlite3.Row = Depends(require("operator"))) -> JSONResponse:
        _check_csrf(request, payload.get(CSRF_FIELD)
                    or request.headers.get("x-csrf-token"))
        if payload.get("password"):
            raise HTTPException(
                400, "continuous workloads must use a SAVED target (its "
                     "password persists in the encrypted store) — a per-job "
                     "password is deleted when the job process ends, so a "
                     "reboot relaunch could never authenticate. Save the "
                     "target under DB targets first.")
        try:
            target_id = int(payload.get("target_id") or 0)
        except (TypeError, ValueError):
            raise HTTPException(400, "target_id must be an integer")
        if not target_id:
            raise HTTPException(400, "target_id (a saved target) is required "
                                     "for continuous workloads")
        tgt = queries.get_target(conn, target_id)
        if tgt is None:
            raise HTTPException(400, "unknown target")
        if not store.get(tgt["password_ref"]):
            raise HTTPException(400, f"saved target '{tgt['name']}' has no "
                                     "stored password — edit it and set one")
        clash = conn.execute(
            f"SELECT id FROM jobs WHERE {_active_clause()} AND target_id=? "
            "LIMIT 1", (target_id,)).fetchone()
        if clash is not None:
            raise HTTPException(409, f"target '{tgt['name']}' already has an "
                                     f"active continuous workload (job "
                                     f"{clash['id']}) — stop it first")
        spec_yaml = build_spec_yaml(tgt, payload)
        v = harness_api.validate_yaml(spec_yaml)
        if not v.get("ok"):
            raise HTTPException(400, v.get("error", "invalid spec"))
        assert v["mode"] == "continuous"
        options = json.dumps({"prepare": bool(payload.get("prepare"))}) \
            if payload.get("prepare") else None
        job_id = queries.enqueue_job(conn, "continuous", spec_yaml, target_id,
                                     user["username"], options=options,
                                     desired_state="running")
        queries.audit(conn, user["username"], "continuous_start",
                      target=tgt["name"], detail=f"job={job_id} {v['workload']}")
        return JSONResponse({"job_id": job_id, "kind": "continuous"})

    @app.get("/api/continuous")
    def cont_list(conn: sqlite3.Connection = Depends(get_conn),
                  user: sqlite3.Row = Depends(require("viewer"))) -> JSONResponse:
        out = []
        for job in conn.execute(
                "SELECT * FROM jobs WHERE kind='continuous' ORDER BY id DESC "
                "LIMIT 200"):
            out.append(_enrich(conn, job, spark=True))
        return JSONResponse(out)

    def _enrich(conn: sqlite3.Connection, job: sqlite3.Row,
                spark: bool = False) -> dict[str, Any]:
        d: dict[str, Any] = {k: job[k] for k in _JOB_FIELDS}
        tgt = queries.get_target(conn, job["target_id"]) if job["target_id"] else None
        d["target_name"] = tgt["name"] if tgt else None
        d["target_host"] = tgt["host"] if tgt else None
        jid = int(job["id"])
        d["last_sample_utc"] = contmetrics.last_sample_utc(conn, jid)
        d["open_outages"] = [dict(r) for r in conn.execute(
            "SELECT id, kind, started_utc, planned FROM outages WHERE job_id=? "
            "AND ended_utc IS NULL", (jid,))]
        d["open_alerts"] = [dict(r) for r in conn.execute(
            "SELECT id, type, severity, fired_utc FROM alerts WHERE job_id=? "
            "AND resolved_utc IS NULL ORDER BY id DESC LIMIT 10", (jid,))]
        if job["run_id"]:
            st = read_state(cfg.results_dir / str(job["run_id"]))
            d["supervisor"] = {k: st.get(k) for k in
                               ("status", "seg", "segments_total", "relaunches",
                                "consecutive_failures", "last_error_class",
                                "updated_utc", "load_stopped")} if st else None
        else:
            d["supervisor"] = None
        try:
            spec = yaml.safe_load(job["spec_yaml"]) or {}
            d["workload_type"] = (spec.get("workload") or {}).get("type", "")
            d["threads"] = (spec.get("continuous") or {}).get("threads")
            d["label"] = (spec.get("run") or {}).get("label", "")
        except yaml.YAMLError:
            d["workload_type"] = ""
            d["threads"] = None
            d["label"] = ""
        if spark:
            t0, t1 = contmetrics.resolve_range("30m")
            ts = contmetrics.timeseries(cfg, conn, jid, t0, t1)
            stride = max(1, len(ts["t"]) // 60)
            d["spark"] = {"t": ts["t"][::stride], "tps": ts["tps_avg"][::stride]}
            s24 = contmetrics.summary(cfg, conn, jid, *contmetrics.resolve_range("24h"))
            d["uptime_24h_pct"] = s24["uptime_pct"]
            d["tps_avg_24h"] = s24["tps_avg"]
        return d

    @app.get("/api/continuous/{job_id}")
    def cont_detail(job_id: int, conn: sqlite3.Connection = Depends(get_conn),
                    user: sqlite3.Row = Depends(require("viewer"))) -> JSONResponse:
        job = _cont_job(conn, job_id)
        d = _enrich(conn, job, spark=False)
        if job["run_id"]:
            man = cfg.results_dir / str(job["run_id"]) / "manifest.json"
            try:
                m = json.loads(man.read_text(encoding="utf-8"))
                d["manifest"] = {"status": m.get("status"),
                                 "continuous": m.get("continuous") or {}}
            except (OSError, ValueError):
                d["manifest"] = None
        return JSONResponse(d)

    @app.post("/api/continuous/{job_id}/stop")
    def cont_stop(job_id: int, request: Request,
                  conn: sqlite3.Connection = Depends(get_conn),
                  user: sqlite3.Row = Depends(require("operator"))) -> JSONResponse:
        _check_csrf(request, request.headers.get("x-csrf-token"))
        job = _cont_job(conn, job_id)
        # Durable intent first, even for jobs stop_job_process won't signal
        # (already terminal but desired running -> the relaunch loop would
        # resurrect them without this).
        queries.update_job(conn, job_id, desired_state="stopped")
        stopping = stop_job_process(cfg, conn, job_id)
        queries.audit(conn, user["username"], "continuous_stop",
                      target=job["run_id"] or f"job:{job_id}")
        return JSONResponse({"stopping": stopping, "desired_state": "stopped"})

    @app.post("/api/continuous/{job_id}/resume")
    def cont_resume(job_id: int, request: Request,
                    conn: sqlite3.Connection = Depends(get_conn),
                    user: sqlite3.Row = Depends(require("operator"))) -> JSONResponse:
        _check_csrf(request, request.headers.get("x-csrf-token"))
        job = _cont_job(conn, job_id)
        if job["state"] in ("queued", "running", "canceling"):
            raise HTTPException(409, "job is already active")
        if not job["target_id"]:
            raise HTTPException(400, "job has no saved target to resume against")
        tgt = queries.get_target(conn, int(job["target_id"]))
        if tgt is None or not store.get(tgt["password_ref"]):
            raise HTTPException(400, "the saved target (or its stored password) "
                                     "is gone — recreate it, then start a new "
                                     "continuous workload")
        clash = conn.execute(
            f"SELECT id FROM jobs WHERE {_active_clause()} AND target_id=? "
            "AND id != ? LIMIT 1", (job["target_id"], job_id)).fetchone()
        if clash is not None:
            raise HTTPException(409, "another continuous workload is already "
                                     f"active on this target (job {clash['id']})")
        queries.update_job(conn, job_id, desired_state="running", state="queued",
                           pid=None, pid_start="", exit_code=None, error="",
                           finished_utc=None)
        queries.audit(conn, user["username"], "continuous_resume",
                      target=job["run_id"] or f"job:{job_id}")
        return JSONResponse({"resumed": True, "job_id": job_id})

    # ── historical metrics ──
    @app.get("/api/continuous/{job_id}/timeseries")
    def cont_timeseries(job_id: int, window: str = "",
                        frm: str = Query("", alias="from"), to: str = "",
                        conn: sqlite3.Connection = Depends(get_conn),
                        user: sqlite3.Row = Depends(require("viewer")),
                        ) -> JSONResponse:
        job = _cont_job(conn, job_id)
        t0, t1 = _range(window, frm, to)
        return JSONResponse(contmetrics.timeseries(
            cfg, conn, job_id, t0, t1, run_id=str(job["run_id"] or "")))

    @app.get("/api/continuous/{job_id}/summary")
    def cont_summary(job_id: int, window: str = "",
                        frm: str = Query("", alias="from"), to: str = "",
                     conn: sqlite3.Connection = Depends(get_conn),
                     user: sqlite3.Row = Depends(require("viewer"))) -> JSONResponse:
        _cont_job(conn, job_id)
        t0, t1 = _range(window, frm, to)
        return JSONResponse(contmetrics.summary(cfg, conn, job_id, t0, t1))

    @app.get("/api/continuous/{job_id}/dbmetrics")
    def cont_dbmetrics(job_id: int, window: str = "",
                        frm: str = Query("", alias="from"), to: str = "",
                       conn: sqlite3.Connection = Depends(get_conn),
                       user: sqlite3.Row = Depends(require("viewer"))) -> JSONResponse:
        _cont_job(conn, job_id)
        t0, t1 = _range(window, frm, to)
        return JSONResponse(contmetrics.db_metrics_series(conn, job_id, t0, t1))

    @app.get("/api/continuous/{job_id}/outages")
    def cont_outages(job_id: int, window: str = "",
                        frm: str = Query("", alias="from"), to: str = "",
                     conn: sqlite3.Connection = Depends(get_conn),
                     user: sqlite3.Row = Depends(require("viewer"))) -> JSONResponse:
        _cont_job(conn, job_id)
        if window or frm:
            t0, t1 = _range(window, frm, to)
            rows = conn.execute(
                "SELECT * FROM outages WHERE job_id=? AND started_utc <= ? AND "
                "(ended_utc IS NULL OR ended_utc >= ?) ORDER BY id DESC LIMIT 1000",
                (job_id, contmetrics._iso_z(t1), contmetrics._iso_z(t0)))
        else:
            rows = conn.execute("SELECT * FROM outages WHERE job_id=? "
                                "ORDER BY id DESC LIMIT 1000", (job_id,))
        return JSONResponse([dict(r) for r in rows])

    @app.get("/api/continuous/{job_id}/alerts")
    def cont_alerts(job_id: int, severity: str = "", type: str = "",
                    limit: int = 200, offset: int = 0,
                    conn: sqlite3.Connection = Depends(get_conn),
                    user: sqlite3.Row = Depends(require("viewer"))) -> JSONResponse:
        _cont_job(conn, job_id)
        rows = alerts.list_alerts(conn, job_id=job_id, severity=severity,
                                  type=type, limit=min(max(limit, 1), 1000),
                                  offset=max(0, offset))
        return JSONResponse([dict(r) for r in rows])

    # ── fleet-wide ledgers ("how often do these fire") ──
    @app.get("/api/alerts")
    def global_alerts(job_id: Optional[int] = None, severity: str = "",
                      type: str = "", since: str = "", unresolved: int = 0,
                      limit: int = 200, offset: int = 0,
                      conn: sqlite3.Connection = Depends(get_conn),
                      user: sqlite3.Row = Depends(require("viewer"))) -> JSONResponse:
        if since:
            try:
                contmetrics._parse_iso(since)
            except contmetrics.RangeError as exc:
                raise HTTPException(400, str(exc))
        rows = alerts.list_alerts(conn, job_id=job_id, severity=severity,
                                  type=type, since_utc=since,
                                  unresolved_only=bool(unresolved),
                                  limit=min(max(limit, 1), 1000),
                                  offset=max(0, offset))
        return JSONResponse([dict(r) for r in rows])

    @app.get("/api/outages")
    def global_outages(job_id: Optional[int] = None, kind: str = "",
                       planned: Optional[int] = None, open_only: int = 0,
                       since: str = "", limit: int = 200, offset: int = 0,
                       conn: sqlite3.Connection = Depends(get_conn),
                       user: sqlite3.Row = Depends(require("viewer"))) -> JSONResponse:
        where, params = ["1=1"], []          # type: ignore[var-annotated]
        if job_id is not None:
            where.append("job_id=?")
            params.append(job_id)
        if kind:
            where.append("kind=?")
            params.append(kind)
        if planned is not None:
            where.append("planned=?")
            params.append(1 if planned else 0)
        if open_only:
            where.append("ended_utc IS NULL")
        if since:
            try:
                contmetrics._parse_iso(since)
            except contmetrics.RangeError as exc:
                raise HTTPException(400, str(exc))
            where.append("started_utc >= ?")
            params.append(since)
        rows = conn.execute(
            f"SELECT * FROM outages WHERE {' AND '.join(where)} "
            "ORDER BY id DESC LIMIT ? OFFSET ?",
            (*params, min(max(limit, 1), 1000), max(0, offset)))
        return JSONResponse([dict(r) for r in rows])

    # ── maintenance windows ──
    @app.get("/api/maintenance")
    def maint_list(job_id: Optional[int] = None,
                   conn: sqlite3.Connection = Depends(get_conn),
                   user: sqlite3.Row = Depends(require("viewer"))) -> JSONResponse:
        if job_id is not None:
            rows = conn.execute(
                "SELECT * FROM maintenance_windows WHERE job_id IS NULL OR "
                "job_id=? ORDER BY starts_utc DESC LIMIT 500", (job_id,))
        else:
            rows = conn.execute("SELECT * FROM maintenance_windows "
                                "ORDER BY starts_utc DESC LIMIT 500")
        return JSONResponse([dict(r) for r in rows])

    @app.post("/api/maintenance")
    def maint_create(request: Request, payload: dict,
                     conn: sqlite3.Connection = Depends(get_conn),
                     user: sqlite3.Row = Depends(require("operator"))) -> JSONResponse:
        _check_csrf(request, payload.get(CSRF_FIELD)
                    or request.headers.get("x-csrf-token"))
        try:
            starts = contmetrics._iso_z(contmetrics._parse_iso(
                str(payload.get("starts_utc", ""))))
            ends = contmetrics._iso_z(contmetrics._parse_iso(
                str(payload.get("ends_utc", ""))))
        except contmetrics.RangeError as exc:
            raise HTTPException(400, str(exc))
        if ends <= starts:
            raise HTTPException(400, "ends_utc must be after starts_utc")
        job_id = payload.get("job_id")
        if job_id is not None:
            try:
                job_id = int(job_id)
            except (TypeError, ValueError):
                raise HTTPException(400, "job_id must be an integer or null")
            _cont_job(conn, job_id)
        cur = conn.execute(
            "INSERT INTO maintenance_windows(job_id, starts_utc, ends_utc, note) "
            "VALUES (?,?,?,?)",
            (job_id, starts, ends, str(payload.get("note", ""))[:500]))
        queries.audit(conn, user["username"], "maintenance_create",
                      target=f"job:{job_id}" if job_id else "global",
                      detail=f"{starts}..{ends}")
        return JSONResponse({"id": int(cur.lastrowid or 0)})

    @app.delete("/api/maintenance/{mid}")
    def maint_delete(mid: int, request: Request,
                     conn: sqlite3.Connection = Depends(get_conn),
                     user: sqlite3.Row = Depends(require("operator"))) -> JSONResponse:
        _check_csrf(request, request.headers.get("x-csrf-token"))
        cur = conn.execute("DELETE FROM maintenance_windows WHERE id=?", (mid,))
        if not cur.rowcount:
            raise HTTPException(404, "maintenance window not found")
        queries.audit(conn, user["username"], "maintenance_delete", target=str(mid))
        return JSONResponse({"deleted": True})
