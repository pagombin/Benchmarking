"""Windowed metrics queries: window->resolution selection, min-preserving
decimation, the 31-day cap, and summary math (uptime/MTBF/MTTR) on synthetic
data."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture()
def qcfg(tmp_path, monkeypatch):
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


def _job(conn) -> int:
    from pgbench_webapp import queries
    return queries.enqueue_job(conn, "continuous", "spec: {}", None, "t",
                               desired_state="running")


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


NOW = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)


def test_resolve_range_presets_and_validation():
    from pgbench_webapp.contmetrics import RangeError, resolve_range
    t0, t1 = resolve_range("10m", now=NOW)
    assert (t1 - t0).total_seconds() == 600 and t1 == NOW
    t0, t1 = resolve_range("30d", now=NOW)
    assert (t1 - t0).days == 30
    t0, t1 = resolve_range("", "2026-03-01T00:00:00Z", "2026-03-01T06:00:00Z")
    assert (t1 - t0).total_seconds() == 6 * 3600
    with pytest.raises(RangeError, match="unknown window"):
        resolve_range("3y")
    with pytest.raises(RangeError, match="after"):
        resolve_range("", "2026-03-02T00:00:00Z", "2026-03-01T00:00:00Z")
    with pytest.raises(RangeError, match="31 days"):
        resolve_range("", "2026-01-01T00:00:00Z", "2026-02-15T00:00:00Z")
    with pytest.raises(RangeError, match="unrecognized"):
        resolve_range("", "yesterday-ish")
    with pytest.raises(RangeError, match="window=|from="):
        resolve_range("")


def test_resolution_selection_1s_vs_1m(qcfg):
    from pgbench_webapp import contmetrics
    cfg = qcfg
    conn = _conn(cfg)
    jid = _job(conn)
    # one hour of 1s samples + matching rollups
    rows = [(jid, _iso(NOW - timedelta(seconds=i)), 100.0, 2000.0, 45.0, 0.0, 0.0)
            for i in range(3600)]
    conn.executemany("INSERT OR REPLACE INTO cont_samples(job_id, ts_utc, tps, "
                     "qps, lat_p99, err_s, reconn_s) VALUES (?,?,?,?,?,?,?)", rows)
    conn.executemany(
        "INSERT OR REPLACE INTO cont_rollup_1m(job_id, ts_utc, n, tps_avg, tps_min, "
        "tps_max, qps_avg, lat_p99_avg, lat_p99_max, err_sum, reconn_sum, gap_s) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [(jid, _iso(NOW - timedelta(minutes=i))[:17] + "00Z", 60, 100.0, 90.0,
          110.0, 2000.0, 45.0, 60.0, 0.0, 0.0, 0) for i in range(600)])
    six_h = contmetrics.timeseries(cfg, conn, jid, NOW - timedelta(hours=6), NOW)
    assert six_h["resolution"] == "1s"
    assert six_h["points"] <= contmetrics.MAX_POINTS
    beyond = contmetrics.timeseries(cfg, conn, jid, NOW - timedelta(hours=7), NOW)
    assert beyond["resolution"] == "1m"
    assert beyond["points"] > 0
    conn.close()


def test_decimation_preserves_minima(qcfg):
    """A single 1-second dip to zero inside 6h of healthy load must survive
    decimation to <= 2000 points (the outage never gets averaged away)."""
    from pgbench_webapp import contmetrics
    cfg = qcfg
    conn = _conn(cfg)
    jid = _job(conn)
    rows = []
    for i in range(6 * 3600):
        tps = 0.0 if i == 5000 else 100.0
        rows.append((jid, _iso(NOW - timedelta(seconds=i)), tps, tps * 20,
                     45.0 if tps else 9000.0, 0.0, 0.0))
    conn.executemany("INSERT OR REPLACE INTO cont_samples(job_id, ts_utc, tps, "
                     "qps, lat_p99, err_s, reconn_s) VALUES (?,?,?,?,?,?,?)", rows)
    ts = contmetrics.timeseries(cfg, conn, jid, NOW - timedelta(hours=6), NOW)
    assert ts["points"] <= contmetrics.MAX_POINTS < 6 * 3600
    assert min(v for v in ts["tps_min"] if v is not None) == 0.0
    assert max(v for v in ts["lat_p99_max"] if v is not None) == 9000.0
    # averages were NOT flattened to the dip
    assert max(v for v in ts["tps_avg"] if v is not None) > 90.0
    conn.close()


def test_timeseries_includes_annotations(qcfg):
    from pgbench_webapp import contmetrics
    cfg = qcfg
    conn = _conn(cfg)
    jid = _job(conn)
    conn.execute("UPDATE jobs SET run_id='r1' WHERE id=?", (jid,))
    conn.execute("INSERT INTO outages(job_id, kind, started_utc, ended_utc, "
                 "duration_s, planned) VALUES (?,?,?,?,?,0)",
                 (jid, "read", _iso(NOW - timedelta(minutes=30)),
                  _iso(NOW - timedelta(minutes=29)), 60.0))
    conn.execute("INSERT INTO maintenance_windows(job_id, starts_utc, ends_utc) "
                 "VALUES (NULL, ?, ?)",
                 (_iso(NOW - timedelta(minutes=10)), _iso(NOW + timedelta(minutes=10))))
    run_dir = cfg.results_dir / "r1"
    run_dir.mkdir(parents=True)
    (run_dir / "events.jsonl").write_text(json.dumps(
        {"ts_utc": _iso(NOW - timedelta(minutes=5)), "type": "loadgen_restart",
         "label": "relaunch #1", "note": "", "source": "auto"}) + "\n")
    ts = contmetrics.timeseries(cfg, conn, jid, NOW - timedelta(hours=1), NOW,
                                run_id="r1")
    assert len(ts["outages"]) == 1 and ts["outages"][0]["kind"] == "read"
    assert len(ts["maintenance"]) == 1
    assert len(ts["events"]) == 1 and ts["events"][0]["type"] == "loadgen_restart"
    conn.close()


def test_union_seconds_overlap():
    from pgbench_webapp.contmetrics import _union_seconds
    assert _union_seconds([]) == 0
    assert _union_seconds([(0, 10), (5, 15)]) == 15          # overlap merged
    assert _union_seconds([(0, 10), (20, 25), (22, 24)]) == 15
    assert _union_seconds([(0, 5), (5, 10)]) == 10


def test_summary_uptime_mtbf_mttr(qcfg):
    from pgbench_webapp import contmetrics
    cfg = qcfg
    conn = _conn(cfg)
    jid = _job(conn)
    t0, t1 = NOW - timedelta(hours=1), NOW

    def outage(kind, start_off, dur, planned=0, open_=False):
        conn.execute(
            "INSERT INTO outages(job_id, kind, started_utc, ended_utc, "
            "duration_s, planned) VALUES (?,?,?,?,?,?)",
            (jid, kind, _iso(t0 + timedelta(seconds=start_off)),
             None if open_ else _iso(t0 + timedelta(seconds=start_off + dur)),
             None if open_ else float(dur), planned))

    outage("read", 300, 120)                    # unplanned db: counts
    outage("write", 1000, 60)                   # unplanned db: counts
    outage("read", 2000, 300, planned=1)        # planned: excluded from uptime
    outage("load", 2500, 100)                   # loadgen-side: excluded
    # samples: a steady 100 tps with known latency spread
    lats = [10.0] * 50 + [100.0] * 45 + [500.0] * 5
    rows = [(jid, _iso(t0 + timedelta(seconds=i + 1)), 100.0, 2000.0,
             lats[i % 100], 0.5, 0.0) for i in range(3600 - 1)]
    conn.executemany("INSERT OR REPLACE INTO cont_samples(job_id, ts_utc, tps, "
                     "qps, lat_p99, err_s, reconn_s) VALUES (?,?,?,?,?,?,?)", rows)

    s = contmetrics.summary(cfg, conn, jid, t0, t1)
    assert s["window_s"] == 3600
    assert s["downtime_s"] == 180.0                       # 120 + 60, unplanned db only
    assert s["uptime_pct"] == pytest.approx(100 * (1 - 180 / 3600), abs=1e-6)
    assert s["outages_total"] == 4
    assert s["outages_unplanned_db"] == 2
    assert s["outages_planned"] == 1
    assert s["outages_load"] == 1
    assert s["mtbf_s"] == 1800.0                          # 3600 / 2 incidents
    assert s["mttr_s"] == 90.0                            # (120 + 60) / 2
    assert s["longest_outage_s"] == 120.0
    assert s["tps_avg"] == 100.0
    assert s["errors_total"] == pytest.approx(0.5 * 3599)
    assert s["lat_p99_p50"] == 100.0 or s["lat_p99_p50"] == 10.0  # median of the mix
    assert s["lat_p99_max"] == 500.0
    conn.close()


def test_summary_open_outage_clipped_to_window(qcfg):
    from pgbench_webapp import contmetrics
    cfg = qcfg
    conn = _conn(cfg)
    jid = _job(conn)
    t0, t1 = NOW - timedelta(hours=1), NOW
    # opened before the window, still open: counts as down for the WHOLE window
    conn.execute("INSERT INTO outages(job_id, kind, started_utc, planned) "
                 "VALUES (?,?,?,0)", (jid, "write", _iso(t0 - timedelta(hours=2))))
    s = contmetrics.summary(cfg, conn, jid, t0, t1)
    assert s["downtime_s"] == 3600.0
    assert s["uptime_pct"] == 0.0
    assert s["outages_open"] == 1
    assert s["mtbf_s"] == 3600.0 and s["mttr_s"] == 3600.0
    conn.close()


def test_summary_clean_window_no_outages(qcfg):
    from pgbench_webapp import contmetrics
    cfg = qcfg
    conn = _conn(cfg)
    jid = _job(conn)
    s = contmetrics.summary(cfg, conn, jid, NOW - timedelta(hours=1), NOW)
    assert s["uptime_pct"] == 100.0
    assert s["mtbf_s"] is None and s["mttr_s"] is None     # nothing to measure
    assert s["outages_total"] == 0
    conn.close()


def test_db_metrics_rates_and_counter_reset(qcfg):
    from pgbench_webapp import contmetrics
    cfg = qcfg
    conn = _conn(cfg)
    jid = _job(conn)
    snaps = [
        (0, {"conn_active": 3, "xact_commit": 1000, "wal_bytes": 10_000,
             "db_size": 5_000_000}),
        (15, {"conn_active": 4, "xact_commit": 1600, "wal_bytes": 40_000,
              "db_size": 5_100_000}),
        (30, {"conn_active": 2, "xact_commit": 100,  # counter RESET (failover)
              "wal_bytes": 41_500, "db_size": 5_100_000}),
    ]
    for off, m in snaps:
        conn.execute("INSERT INTO cont_db_metrics(job_id, ts_utc, metrics) "
                     "VALUES (?,?,?)",
                     (jid, _iso(NOW + timedelta(seconds=off)), json.dumps(m)))
    out = contmetrics.db_metrics_series(conn, jid, NOW - timedelta(minutes=1),
                                        NOW + timedelta(minutes=1))
    assert out["points"] == 3
    assert out["conn_active"] == [3, 4, 2]
    assert out["xact_commit_rate"] == [None, 40.0, None]   # reset -> None, not negative
    assert out["wal_bytes_rate"] == [None, 2000.0, 100.0]
    assert out["db_size"] == [5_000_000, 5_100_000, 5_100_000]
    conn.close()
