"""Continuous-mode historical metrics: ingest, 1-minute rollups, retention,
and the rebuild path.

Source-of-truth contract: the run directory's RAW timestamped segment logs
(``raw/cont_seg*.log``, ``<ISO-UTC>\\t<sysbench line>``) are canonical. The
SQLite tables here are a queryable INDEX over them:

* ``cont_samples``   — 1-second samples, kept for a short raw horizon (72h).
* ``cont_rollup_1m`` — minute aggregates incl. ``gap_s`` (seconds in the
  minute with NO sample — downtime shows as gaps), kept for the full horizon.
* ``cont_cursors``   — (job, segment) byte offsets so an ingester restart
  never re-reads history; the samples PRIMARY KEY is the hard dedup, so even
  a lost cursor only costs a re-scan, never duplicate rows.

The ingester tails the raw segment logs directly (not the convenience CSV):
segments are append-only and pruned wholesale, so a byte cursor into one can
never be silently invalidated by a rewrite. ``reindex_continuous`` proves the
invariant: wipe and rebuild a job's index from whatever raw segments remain
on disk (older rollup minutes, whose raw was already pruned, are preserved).
"""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from pgbench_harness.parser import parse_interval_line
from pgbench_webapp import queries
from pgbench_webapp.config import Config

# Settings keys (tunable via /api/admin/settings) and their defaults.
RETENTION_RAW_H_DEFAULT = 72          # cont_samples + raw segment logs
RETENTION_DAYS_DEFAULT = 35           # rollups, db metrics, resolved alerts, outages
INGEST_BATCH_LINES = 20000            # bound one tick's parse/insert work


def _setting_int(conn: sqlite3.Connection, key: str, default: int) -> int:
    try:
        v = int(queries.get_setting(conn, key, str(default)) or default)
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


def _iso_z(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def minute_floor(ts_utc: str) -> str:
    """'2026-08-20T12:34:56Z' (or with fractional seconds) -> minute floor."""
    return ts_utc[:17] + "00Z"


def _second(ts_utc: str) -> str:
    """Normalize a raw read-time stamp to second precision (the sample key)."""
    return ts_utc[:19] + "Z"


# ── ingest ──────────────────────────────────────────────────────────

def _cursor(conn: sqlite3.Connection, job_id: int, seg: str) -> int:
    row = conn.execute("SELECT pos FROM cont_cursors WHERE job_id=? AND seg=?",
                       (job_id, seg)).fetchone()
    return int(row["pos"]) if row else 0


def _save_cursor(conn: sqlite3.Connection, job_id: int, seg: str, pos: int) -> None:
    conn.execute("INSERT INTO cont_cursors(job_id, seg, pos) VALUES (?,?,?) "
                 "ON CONFLICT(job_id, seg) DO UPDATE SET pos=excluded.pos",
                 (job_id, seg, pos))


def _parse_segment_lines(text: str, seg: str) -> list[tuple[str, Any]]:
    """(second_ts, IntervalSample) rows from raw '<ISO>\\t<line>' text."""
    out: list[tuple[str, Any]] = []
    for raw in text.split("\n"):
        ts, _, rest = raw.partition("\t")
        if not rest or len(ts) < 20 or not ts.startswith("20"):
            continue
        s = parse_interval_line(rest)
        if s is None:
            continue
        out.append((_second(ts), s))
    return out


def _insert_samples(conn: sqlite3.Connection, job_id: int, seg: str,
                    rows: list[tuple[str, Any]]) -> set[str]:
    """INSERT OR IGNORE the batch; returns the touched minute floors."""
    if not rows:
        return set()
    conn.executemany(
        "INSERT OR IGNORE INTO cont_samples(job_id, ts_utc, tps, qps, qps_r, "
        "qps_w, qps_o, lat_p99, err_s, reconn_s, threads, seg) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [(job_id, ts, s.tps, s.qps, s.r, s.w, s.o, s.lat_ms, s.err_s,
          s.reconn_s, s.threads, seg) for ts, s in rows])
    return {minute_floor(ts) for ts, _ in rows}


def rollup_minutes(conn: sqlite3.Connection, job_id: int,
                   minutes: Iterable[str]) -> None:
    """(Re)compute the 1m rollup for each touched minute from cont_samples.

    Idempotent: recomputing a minute after more of its samples arrive
    self-corrects ``gap_s``. Only minutes still covered by the raw-samples
    horizon are ever recomputed (callers pass minutes of NEW samples), so
    retention pruning of old samples can never zero out old rollups.
    """
    for m in sorted(set(minutes)):
        try:
            end = _iso_z(datetime.strptime(m, "%Y-%m-%dT%H:%M:%SZ")
                         .replace(tzinfo=timezone.utc) + timedelta(minutes=1))
        except ValueError:
            continue
        agg = conn.execute(
            "SELECT count(*) AS n, avg(tps) AS tps_avg, min(tps) AS tps_min, "
            "max(tps) AS tps_max, avg(qps) AS qps_avg, "
            "avg(lat_p99) AS lat_avg, max(lat_p99) AS lat_max, "
            "sum(err_s) AS err_sum, sum(reconn_s) AS reconn_sum "
            "FROM cont_samples WHERE job_id=? AND ts_utc >= ? AND ts_utc < ?",
            (job_id, m, end)).fetchone()
        n = int(agg["n"] or 0)
        if n == 0:
            continue                     # an all-gap minute has no row at all
        conn.execute(
            "INSERT INTO cont_rollup_1m(job_id, ts_utc, n, tps_avg, tps_min, "
            "tps_max, qps_avg, lat_p99_avg, lat_p99_max, err_sum, reconn_sum, gap_s) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(job_id, ts_utc) DO UPDATE SET n=excluded.n, "
            "tps_avg=excluded.tps_avg, tps_min=excluded.tps_min, "
            "tps_max=excluded.tps_max, qps_avg=excluded.qps_avg, "
            "lat_p99_avg=excluded.lat_p99_avg, lat_p99_max=excluded.lat_p99_max, "
            "err_sum=excluded.err_sum, reconn_sum=excluded.reconn_sum, "
            "gap_s=excluded.gap_s",
            (job_id, m, n, agg["tps_avg"], agg["tps_min"], agg["tps_max"],
             agg["qps_avg"], agg["lat_avg"], agg["lat_max"],
             agg["err_sum"], agg["reconn_sum"], max(0, 60 - n)))


def ingest_job(cfg: Config, conn: sqlite3.Connection, job_id: int,
               run_id: str) -> int:
    """Tail every raw segment of one job's run dir from its cursor; insert new
    samples and refresh the touched rollup minutes. Returns new-sample count.

    Restart-safe: cursors persist per (job, segment); the samples PK makes a
    replay after a lost cursor idempotent. A partial trailing line (mid-append)
    is left for the next tick.
    """
    raw_dir = cfg.results_dir / run_id / "raw"
    if not raw_dir.is_dir():
        return 0
    total = 0
    touched: set[str] = set()
    for seg_log in sorted(raw_dir.glob("cont_seg*.log")):
        seg = seg_log.stem
        try:
            size = seg_log.stat().st_size
        except OSError:
            continue
        pos = _cursor(conn, job_id, seg)
        if pos > size:
            pos = 0                       # defensive: file replaced/truncated
        if pos >= size:
            continue
        try:
            with open(seg_log, "rb") as fh:
                fh.seek(pos)
                data = fh.read(min(size - pos, INGEST_BATCH_LINES * 256))
        except OSError:
            continue
        nl = data.rfind(b"\n")
        if nl == -1:
            continue                      # only a partial line so far
        chunk = data[:nl + 1]
        rows = _parse_segment_lines(chunk.decode("utf-8", "replace"), seg)
        touched |= _insert_samples(conn, job_id, seg, rows)
        _save_cursor(conn, job_id, seg, pos + len(chunk))
        total += len(rows)
    if touched:
        rollup_minutes(conn, job_id, touched)
    return total


def last_sample_utc(conn: sqlite3.Connection, job_id: int) -> str:
    row = conn.execute("SELECT max(ts_utc) AS m FROM cont_samples WHERE job_id=?",
                       (job_id,)).fetchone()
    return str(row["m"] or "")


# ── retention ───────────────────────────────────────────────────────

def _batched_delete(conn: sqlite3.Connection, table: str, where: str,
                    params: tuple, batch: int = 5000) -> int:
    """Bounded deletes so retention never holds a multi-second write lock
    against a hot ingester. Uses the PK-ordered ts_utc bound (the tables are
    WITHOUT ROWID); loops until the backlog is gone."""
    deleted = 0
    while True:
        row = conn.execute(
            f"SELECT ts_utc FROM {table} WHERE {where} "
            "ORDER BY job_id, ts_utc LIMIT 1 OFFSET ?",
            (*params, batch - 1)).fetchone()
        if row is None:                   # tail batch — delete what's left
            cur = conn.execute(f"DELETE FROM {table} WHERE {where}", params)
            return deleted + int(cur.rowcount or 0)
        cur = conn.execute(
            f"DELETE FROM {table} WHERE {where} AND ts_utc <= ?",
            (*params, row["ts_utc"]))
        deleted += int(cur.rowcount or 0)


def prune(cfg: Config, conn: sqlite3.Connection) -> dict[str, int]:
    """Apply the retention policy. Called from worker housekeeping.

    * cont_samples older than ``cont_retention_raw_h`` (default 72h)
    * cont_rollup_1m / cont_db_metrics / resolved alerts / closed outages
      older than ``cont_retention_days`` (default 35d)
    * raw segment logs older than the raw horizon, only once fully ingested
      (cursor at EOF) — events.jsonl and state.json are kept forever.
    """
    now = datetime.now(timezone.utc)
    raw_h = _setting_int(conn, "cont_retention_raw_h", RETENTION_RAW_H_DEFAULT)
    days = _setting_int(conn, "cont_retention_days", RETENTION_DAYS_DEFAULT)
    raw_cut = _iso_z(now - timedelta(hours=raw_h))
    day_cut = _iso_z(now - timedelta(days=days))
    out = {
        "samples": _batched_delete(conn, "cont_samples", "ts_utc < ?", (raw_cut,)),
        "rollups": _batched_delete(conn, "cont_rollup_1m", "ts_utc < ?", (day_cut,)),
        "db_metrics": _batched_delete(conn, "cont_db_metrics", "ts_utc < ?", (day_cut,)),
    }
    cur = conn.execute("DELETE FROM alerts WHERE resolved_utc IS NOT NULL "
                       "AND fired_utc < ?", (day_cut,))
    out["alerts"] = int(cur.rowcount or 0)
    cur = conn.execute("DELETE FROM outages WHERE ended_utc IS NOT NULL "
                       "AND started_utc < ?", (day_cut,))
    out["outages"] = int(cur.rowcount or 0)
    out["segments"] = _prune_raw_segments(cfg, conn, raw_cut)
    return out


def _prune_raw_segments(cfg: Config, conn: sqlite3.Connection,
                        raw_cut: str) -> int:
    """Delete raw segment logs whose entire content is older than the raw
    horizon AND fully ingested. The newest segment is always kept (it may be
    the live one). events.jsonl / state.json / manifest are never touched."""
    cutoff_epoch = datetime.strptime(raw_cut, "%Y-%m-%dT%H:%M:%SZ") \
        .replace(tzinfo=timezone.utc).timestamp()
    pruned = 0
    for job in conn.execute("SELECT id, run_id FROM jobs WHERE kind='continuous' "
                            "AND run_id IS NOT NULL"):
        raw_dir = cfg.results_dir / str(job["run_id"]) / "raw"
        if not raw_dir.is_dir():
            continue
        segs = sorted(raw_dir.glob("cont_seg*.log"))
        for seg_log in segs[:-1]:                      # never the newest
            try:
                st = seg_log.stat()
            except OSError:
                continue
            if st.st_mtime >= cutoff_epoch:
                continue                               # still inside the horizon
            if _cursor(conn, int(job["id"]), seg_log.stem) < st.st_size:
                continue                               # not fully ingested yet
            try:
                seg_log.unlink()
            except OSError:
                continue
            conn.execute("DELETE FROM cont_cursors WHERE job_id=? AND seg=?",
                         (job["id"], seg_log.stem))
            pruned += 1
    return pruned


_last_checkpoint = 0.0


def maybe_wal_checkpoint(conn: sqlite3.Connection, every_s: float = 3600.0) -> bool:
    """Periodic PRAGMA wal_checkpoint(TRUNCATE) so the WAL file cannot grow
    without bound under the ingester's constant write load."""
    global _last_checkpoint
    if time.monotonic() - _last_checkpoint < every_s:
        return False
    _last_checkpoint = time.monotonic()
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.Error:
        pass
    return True


# ── rebuild path (invariant: SQLite is a rebuildable index) ─────────

def reindex_continuous(cfg: Config, conn: sqlite3.Connection, job_id: int) -> dict[str, int]:
    """Wipe and rebuild one job's samples/rollups/cursors from the raw segment
    logs still on disk. Rollup minutes older than the surviving raw coverage
    are preserved (their raw was legitimately pruned)."""
    job = queries.get_job(conn, job_id)
    if job is None or job["kind"] != "continuous" or not job["run_id"]:
        raise ValueError(f"job {job_id} is not a continuous job with a run")
    conn.execute("DELETE FROM cont_samples WHERE job_id=?", (job_id,))
    conn.execute("DELETE FROM cont_cursors WHERE job_id=?", (job_id,))
    inserted = 0
    first_ts: Optional[str] = None
    raw_dir = cfg.results_dir / str(job["run_id"]) / "raw"
    touched: set[str] = set()
    for seg_log in sorted(raw_dir.glob("cont_seg*.log")) if raw_dir.is_dir() else []:
        try:
            text = seg_log.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rows = _parse_segment_lines(text, seg_log.stem)
        if rows:
            seg_min = min(ts for ts, _ in rows)
            first_ts = seg_min if first_ts is None else min(first_ts, seg_min)
        touched |= _insert_samples(conn, job_id, seg_log.stem, rows)
        try:
            _save_cursor(conn, job_id, seg_log.stem, seg_log.stat().st_size)
        except OSError:
            pass
        inserted += len(rows)
    if first_ts is not None:
        # rebuild only the rollup span the raw logs can vouch for
        conn.execute("DELETE FROM cont_rollup_1m WHERE job_id=? AND ts_utc >= ?",
                     (job_id, minute_floor(first_ts)))
        rollup_minutes(conn, job_id, touched)
    return {"samples": inserted, "minutes": len(touched)}
