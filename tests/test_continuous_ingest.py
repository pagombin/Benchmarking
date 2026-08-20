"""Continuous metrics pipeline: raw-log tail -> cont_samples -> 1m rollups,
cursor idempotency across ingester restarts, retention, and reindex-from-raw."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


@pytest.fixture()
def mcfg(tmp_path, monkeypatch):
    data = tmp_path / "data"
    monkeypatch.setenv("PGBENCH_DATA_DIR", str(data))
    monkeypatch.setenv("PGBENCH_DB", str(data / "pgbench.db"))
    from pgbench_webapp.config import ensure_dirs, load_config
    from pgbench_webapp.db import migrate
    cfg = load_config()
    ensure_dirs(cfg)
    migrate(cfg.db_path)
    return cfg


def _conn(cfg):
    from pgbench_webapp.db import connect
    return connect(cfg.db_path)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "000Z"


def _line(ts: datetime, tps: float = 100.0, threads: int = 4,
          err: float = 0.0, reconn: float = 0.0) -> str:
    qps = tps * 20
    return (f"{_iso(ts)}\t[ 1s ] thds: {threads} tps: {tps:.2f} qps: {qps:.2f} "
            f"(r/w/o: {qps * 0.5:.2f}/{qps * 0.3:.2f}/{qps * 0.2:.2f}) "
            f"lat (ms,99%): 45.00 err/s {err:.2f} reconn/s: {reconn:.2f}\n")


def _mk_job(cfg, conn, run_id: str = "cont-run") -> int:
    from pgbench_webapp import queries
    (cfg.results_dir / run_id / "raw").mkdir(parents=True, exist_ok=True)
    jid = queries.enqueue_job(conn, "continuous", "spec: {}", None, "t",
                              desired_state="running")
    queries.update_job(conn, jid, run_id=run_id, state="running")
    return jid


BASE = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _write_seg(cfg, run_id: str, seg: str, start: datetime, seconds: int,
               tps: float = 100.0) -> Path:
    p = cfg.results_dir / run_id / "raw" / f"{seg}.log"
    with open(p, "a", encoding="utf-8") as fh:
        for i in range(seconds):
            fh.write(_line(start + timedelta(seconds=i), tps=tps))
    return p


def test_ingest_rollups_and_gap_s(mcfg):
    """A minute with 20 samples rolls up with gap_s=40; full minutes gap_s=0;
    an untouched minute has NO rollup row (absence IS the gap signal)."""
    from pgbench_webapp import contmetrics
    cfg = mcfg
    conn = _conn(cfg)
    jid = _mk_job(cfg, conn)
    # 12:00:40..12:00:59 (20 samples), 12:01 full, 12:02 skipped, 12:03:00..29
    _write_seg(cfg, "cont-run", "cont_seg0001",
               BASE + timedelta(seconds=40), 20)
    _write_seg(cfg, "cont-run", "cont_seg0001",
               BASE + timedelta(minutes=1), 60)
    _write_seg(cfg, "cont-run", "cont_seg0002",
               BASE + timedelta(minutes=3), 30)
    n = contmetrics.ingest_job(cfg, conn, jid, "cont-run")
    assert n == 110
    rows = {r["ts_utc"]: r for r in conn.execute(
        "SELECT * FROM cont_rollup_1m WHERE job_id=? ORDER BY ts_utc", (jid,))}
    assert rows["2026-01-01T12:00:00Z"]["n"] == 20
    assert rows["2026-01-01T12:00:00Z"]["gap_s"] == 40
    assert rows["2026-01-01T12:01:00Z"]["gap_s"] == 0
    assert "2026-01-01T12:02:00Z" not in rows          # all-gap minute: no row
    assert rows["2026-01-01T12:03:00Z"]["n"] == 30
    assert rows["2026-01-01T12:01:00Z"]["tps_avg"] == pytest.approx(100.0)
    conn.close()


def test_ingest_idempotent_across_cursor_loss(mcfg):
    """Losing every cursor (ingester restart worst case) re-scans but never
    double-inserts; with cursors intact a re-ingest reads nothing at all."""
    from pgbench_webapp import contmetrics
    cfg = mcfg
    conn = _conn(cfg)
    jid = _mk_job(cfg, conn)
    _write_seg(cfg, "cont-run", "cont_seg0001", BASE, 90)
    assert contmetrics.ingest_job(cfg, conn, jid, "cont-run") == 90
    count = conn.execute("SELECT count(*) FROM cont_samples WHERE job_id=?",
                         (jid,)).fetchone()[0]
    assert count == 90
    # cursor intact -> nothing new to read
    assert contmetrics.ingest_job(cfg, conn, jid, "cont-run") == 0
    # cursor lost -> full re-scan, but the PK dedups every row
    conn.execute("DELETE FROM cont_cursors WHERE job_id=?", (jid,))
    contmetrics.ingest_job(cfg, conn, jid, "cont-run")
    assert conn.execute("SELECT count(*) FROM cont_samples WHERE job_id=?",
                        (jid,)).fetchone()[0] == count
    # incremental append picks up ONLY the new tail
    _write_seg(cfg, "cont-run", "cont_seg0001",
               BASE + timedelta(seconds=90), 10)
    assert contmetrics.ingest_job(cfg, conn, jid, "cont-run") == 10
    conn.close()


def test_partial_trailing_line_waits_for_completion(mcfg):
    from pgbench_webapp import contmetrics
    cfg = mcfg
    conn = _conn(cfg)
    jid = _mk_job(cfg, conn)
    p = _write_seg(cfg, "cont-run", "cont_seg0001", BASE, 5)
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(_line(BASE + timedelta(seconds=5)).rstrip("\n"))  # no newline yet
    assert contmetrics.ingest_job(cfg, conn, jid, "cont-run") == 5
    with open(p, "a", encoding="utf-8") as fh:
        fh.write("\n")
    assert contmetrics.ingest_job(cfg, conn, jid, "cont-run") == 1
    conn.close()


def test_clock_skew_backwards_does_not_corrupt_rollups(mcfg):
    """Samples with timestamps EARLIER than already-ingested ones (clock step
    across a reboot) still land and their minutes recompute correctly."""
    from pgbench_webapp import contmetrics
    cfg = mcfg
    conn = _conn(cfg)
    jid = _mk_job(cfg, conn)
    _write_seg(cfg, "cont-run", "cont_seg0002", BASE + timedelta(minutes=2), 60)
    contmetrics.ingest_job(cfg, conn, jid, "cont-run")
    # a second segment arrives bearing OLDER stamps (skewed clock)
    _write_seg(cfg, "cont-run", "cont_seg0003", BASE, 60)
    contmetrics.ingest_job(cfg, conn, jid, "cont-run")
    rows = {r["ts_utc"]: r for r in conn.execute(
        "SELECT * FROM cont_rollup_1m WHERE job_id=?", (jid,))}
    assert rows["2026-01-01T12:00:00Z"]["n"] == 60
    assert rows["2026-01-01T12:02:00Z"]["n"] == 60
    assert all(r["gap_s"] >= 0 for r in rows.values())
    conn.close()


def test_retention_prunes_by_horizon(mcfg):
    from pgbench_webapp import alerts, contmetrics
    cfg = mcfg
    conn = _conn(cfg)
    jid = _mk_job(cfg, conn)
    now = datetime.now(timezone.utc)
    old = now - timedelta(hours=100)          # beyond the 72h raw horizon
    ancient = now - timedelta(days=40)        # beyond the 35d horizon
    _write_seg(cfg, "cont-run", "cont_seg0001", old, 30)
    _write_seg(cfg, "cont-run", "cont_seg0002", now - timedelta(seconds=60), 30)
    contmetrics.ingest_job(cfg, conn, jid, "cont-run")
    # ancient rollup + ledger rows
    conn.execute("INSERT INTO cont_rollup_1m(job_id, ts_utc, n, gap_s) VALUES (?,?,?,?)",
                 (jid, ancient.strftime("%Y-%m-%dT%H:%M:00Z"), 60, 0))
    aid = alerts.store_alert(conn, type="latency", severity="warn",
                             dedup_key=f"job:{jid}:latency", job_id=jid)
    conn.execute("UPDATE alerts SET fired_utc=?, resolved_utc=? WHERE id=?",
                 (ancient.strftime("%Y-%m-%dT%H:%M:%SZ"),
                  ancient.strftime("%Y-%m-%dT%H:%M:%SZ"), aid))
    alerts.store_alert(conn, type="db_unreachable", severity="crit",
                       dedup_key=f"job:{jid}:db_unreachable", job_id=jid)
    conn.execute("INSERT INTO outages(job_id, kind, started_utc, ended_utc, duration_s) "
                 "VALUES (?,?,?,?,?)",
                 (jid, "read", ancient.strftime("%Y-%m-%dT%H:%M:%SZ"),
                  ancient.strftime("%Y-%m-%dT%H:%M:%SZ"), 30.0))
    conn.execute("INSERT INTO outages(job_id, kind, started_utc) VALUES (?,?,?)",
                 (jid, "write", ancient.strftime("%Y-%m-%dT%H:%M:%SZ")))  # still open

    out = contmetrics.prune(cfg, conn)
    assert out["samples"] == 30                        # only the old half
    left = [r["ts_utc"] for r in conn.execute(
        "SELECT ts_utc FROM cont_samples WHERE job_id=?", (jid,))]
    assert len(left) == 30 and all(t > old.strftime("%Y-%m-%dT%H:%M:%SZ") for t in left)
    assert conn.execute("SELECT count(*) FROM cont_rollup_1m WHERE ts_utc < ?",
                        ((now - timedelta(days=36)).strftime("%Y-%m-%dT%H:%M:%SZ"),)
                        ).fetchone()[0] == 0            # ancient rollup pruned
    kinds = [r["type"] for r in conn.execute("SELECT type FROM alerts")]
    assert "latency" not in kinds                       # resolved + ancient: pruned
    assert "db_unreachable" in kinds                    # unresolved: kept
    outs = [r["kind"] for r in conn.execute("SELECT kind FROM outages")]
    assert outs == ["write"]                            # open outage survives
    conn.close()


def test_raw_segment_pruning_requires_full_ingest(mcfg):
    from pgbench_webapp import contmetrics
    cfg = mcfg
    conn = _conn(cfg)
    jid = _mk_job(cfg, conn)
    now = datetime.now(timezone.utc)
    old_ts = now - timedelta(hours=100)
    s1 = _write_seg(cfg, "cont-run", "cont_seg0001", old_ts, 10)
    s2 = _write_seg(cfg, "cont-run", "cont_seg0002", old_ts, 10)
    s3 = _write_seg(cfg, "cont-run", "cont_seg0003", now, 10)   # newest — never pruned
    contmetrics.ingest_job(cfg, conn, jid, "cont-run")
    # age the files on disk; make seg0002 look NOT fully ingested
    stale = (now - timedelta(hours=100)).timestamp()
    for p in (s1, s2):
        os.utime(p, (stale, stale))
    conn.execute("UPDATE cont_cursors SET pos = pos - 5 WHERE job_id=? AND seg=?",
                 (jid, "cont_seg0002"))
    pruned = contmetrics.prune(cfg, conn)["segments"]
    assert pruned == 1
    assert not s1.exists()                     # old + fully ingested: pruned
    assert s2.exists()                         # old but not fully ingested: kept
    assert s3.exists()                         # newest: kept
    # its cursor row went with it
    assert contmetrics._cursor(conn, jid, "cont_seg0001") == 0
    conn.close()


def test_reindex_rebuilds_identically_and_keeps_older_rollups(mcfg):
    """reindex-from-raw == original ingest (the rebuildability invariant), and
    rollup minutes older than the surviving raw coverage are preserved."""
    from pgbench_webapp import contmetrics
    cfg = mcfg
    conn = _conn(cfg)
    jid = _mk_job(cfg, conn)
    _write_seg(cfg, "cont-run", "cont_seg0001", BASE, 90, tps=120.0)
    _write_seg(cfg, "cont-run", "cont_seg0002",
               BASE + timedelta(minutes=5), 60, tps=80.0)
    contmetrics.ingest_job(cfg, conn, jid, "cont-run")
    orig_samples = list(conn.execute(
        "SELECT * FROM cont_samples WHERE job_id=? ORDER BY ts_utc", (jid,)))
    orig_rollups = list(conn.execute(
        "SELECT * FROM cont_rollup_1m WHERE job_id=? ORDER BY ts_utc", (jid,)))
    # a rollup whose raw was already pruned (before raw coverage) must survive
    conn.execute("INSERT INTO cont_rollup_1m(job_id, ts_utc, n, tps_avg, gap_s) "
                 "VALUES (?,?,?,?,?)", (jid, "2025-12-01T00:00:00Z", 60, 99.0, 0))
    # sabotage the index: bogus + missing rows
    conn.execute("DELETE FROM cont_samples WHERE job_id=? AND ts_utc LIKE '%:00:1%'",
                 (jid,))
    conn.execute("INSERT OR REPLACE INTO cont_samples(job_id, ts_utc, tps) "
                 "VALUES (?,?,?)", (jid, "2026-01-01T12:00:30Z", 424242.0))
    conn.execute("UPDATE cont_rollup_1m SET tps_avg=0 WHERE job_id=? AND ts_utc=?",
                 (jid, "2026-01-01T12:01:00Z"))

    out = contmetrics.reindex_continuous(cfg, conn, jid)
    assert out["samples"] == 150
    rebuilt = list(conn.execute(
        "SELECT * FROM cont_samples WHERE job_id=? ORDER BY ts_utc", (jid,)))
    assert [tuple(r) for r in rebuilt] == [tuple(r) for r in orig_samples]
    rebuilt_r = list(conn.execute(
        "SELECT * FROM cont_rollup_1m WHERE job_id=? AND ts_utc >= '2026' "
        "ORDER BY ts_utc", (jid,)))
    assert [tuple(r) for r in rebuilt_r] == [tuple(r) for r in orig_rollups]
    old = conn.execute("SELECT tps_avg FROM cont_rollup_1m WHERE job_id=? AND "
                       "ts_utc='2025-12-01T00:00:00Z'", (jid,)).fetchone()
    assert old is not None and old["tps_avg"] == 99.0
    conn.close()


def test_reindex_cli_entrypoint(mcfg):
    from pgbench_webapp import contmetrics
    from pgbench_webapp.cli_entry import web_main
    cfg = mcfg
    conn = _conn(cfg)
    jid = _mk_job(cfg, conn)
    _write_seg(cfg, "cont-run", "cont_seg0001", BASE, 30)
    conn.close()
    assert web_main(["reindex-continuous", "--job", str(jid)]) == 0
    conn = _conn(cfg)
    assert conn.execute("SELECT count(*) FROM cont_samples WHERE job_id=?",
                        (jid,)).fetchone()[0] == 30
    conn.close()
    assert web_main(["reindex-continuous", "--job", "99999"]) == 2
    assert web_main(["reindex-continuous"]) == 2
    # `contmetrics.last_sample_utc` helper answers from the rebuilt index
    conn = _conn(cfg)
    assert contmetrics.last_sample_utc(conn, jid).startswith("2026-01-01T12:00:29")
    conn.close()
