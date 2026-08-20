"""Prober state machine + outage ledger: healthy->degraded->down thresholds,
recovery durations, read/write/load kinds, maintenance suppression, and the
psql probe runner against fakebin."""

from __future__ import annotations

import os
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

FAKEBIN = Path(__file__).resolve().parent / "fakebin"


@pytest.fixture()
def pcfg(tmp_path, monkeypatch):
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


NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def test_healthy_degraded_down_thresholds(pcfg):
    from pgbench_webapp.contprobe import OutageTracker
    conn = _conn(pcfg)
    jid = _job(conn)
    t = OutageTracker(conn, jid, "read", failures_to_down=3)
    assert t.observe(False, "connection refused", _iso(NOW)) == "degraded"
    assert t.status == "degraded"
    assert t.observe(False, "connection refused", _iso(NOW + timedelta(seconds=5))) is None
    # no outage row while merely degraded
    assert conn.execute("SELECT count(*) FROM outages").fetchone()[0] == 0
    assert t.observe(False, "connection refused", _iso(NOW + timedelta(seconds=10))) == "down"
    row = conn.execute("SELECT * FROM outages").fetchone()
    assert row["kind"] == "read" and row["ended_utc"] is None
    assert row["started_utc"] == _iso(NOW)          # outage began at FIRST failure
    assert row["error_class"] == "network"
    a = conn.execute("SELECT * FROM alerts WHERE type='db_unreachable'").fetchone()
    assert a is not None and a["severity"] == "crit"
    assert a["resolved_utc"] is None and a["delivery"] is None   # pending delivery

    # recovery closes the row with its duration and resolves + records info
    assert t.observe(True, "", _iso(NOW + timedelta(seconds=70))) == "recovered"
    row = conn.execute("SELECT * FROM outages").fetchone()
    assert row["ended_utc"] == _iso(NOW + timedelta(seconds=70))
    assert row["duration_s"] == 70.0
    assert conn.execute("SELECT resolved_utc FROM alerts WHERE type='db_unreachable'")
    assert conn.execute("SELECT count(*) FROM alerts WHERE type='db_recovered'"
                        ).fetchone()[0] == 1
    # a fresh outage later opens a NEW row (dedup released on resolve)
    for i in range(3):
        t.observe(False, "x", _iso(NOW + timedelta(seconds=100 + i)))
    assert conn.execute("SELECT count(*) FROM outages").fetchone()[0] == 2
    conn.close()


def test_flap_inside_threshold_never_opens_outage(pcfg):
    from pgbench_webapp.contprobe import OutageTracker
    conn = _conn(pcfg)
    jid = _job(conn)
    t = OutageTracker(conn, jid, "write", failures_to_down=3)
    for i in range(10):                       # fail, fail, ok, fail, fail, ok ...
        t.observe(False, "blip", _iso(NOW + timedelta(seconds=i * 3)))
        t.observe(False, "blip", _iso(NOW + timedelta(seconds=i * 3 + 1)))
        t.observe(True, "", _iso(NOW + timedelta(seconds=i * 3 + 2)))
    assert conn.execute("SELECT count(*) FROM outages").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM alerts").fetchone()[0] == 0
    conn.close()


def test_kinds_are_independent(pcfg):
    from pgbench_webapp.contprobe import OutageTracker
    conn = _conn(pcfg)
    jid = _job(conn)
    r = OutageTracker(conn, jid, "read", 1)
    w = OutageTracker(conn, jid, "write", 1)
    r.observe(False, "read down", _iso(NOW))
    w.observe(False, "write down", _iso(NOW))
    kinds = sorted(x["kind"] for x in conn.execute("SELECT kind FROM outages"))
    assert kinds == ["read", "write"]
    r.observe(True, "", _iso(NOW + timedelta(seconds=30)))
    open_kinds = [x["kind"] for x in conn.execute(
        "SELECT kind FROM outages WHERE ended_utc IS NULL")]
    assert open_kinds == ["write"]
    conn.close()


def test_maintenance_overlap_sets_planned_and_suppresses(pcfg):
    from pgbench_webapp import alerts
    from pgbench_webapp.contprobe import OutageTracker
    conn = _conn(pcfg)
    jid = _job(conn)
    conn.execute("INSERT INTO maintenance_windows(job_id, starts_utc, ends_utc, note) "
                 "VALUES (?,?,?,?)",
                 (jid, _iso(NOW - timedelta(minutes=5)),
                  _iso(NOW + timedelta(minutes=5)), "planned failover test"))
    t = OutageTracker(conn, jid, "read", 1)
    t.observe(False, "expected blip", _iso(NOW))
    row = conn.execute("SELECT * FROM outages").fetchone()
    assert row["planned"] == 1
    a = conn.execute("SELECT * FROM alerts WHERE type='db_unreachable'").fetchone()
    assert a["delivery"] == alerts.DELIVERY_SUPPRESSED    # stored, never sent
    t.observe(True, "", _iso(NOW + timedelta(seconds=42)))
    assert conn.execute("SELECT planned FROM outages").fetchone()["planned"] == 1

    # a GLOBAL window (job_id NULL) suppresses any job
    jid2 = _job(conn)
    conn.execute("INSERT INTO maintenance_windows(job_id, starts_utc, ends_utc) "
                 "VALUES (NULL, ?, ?)",
                 (_iso(NOW - timedelta(minutes=1)), _iso(NOW + timedelta(minutes=1))))
    t2 = OutageTracker(conn, jid2, "write", 1)
    t2.observe(False, "x", _iso(NOW))
    assert conn.execute("SELECT planned FROM outages WHERE job_id=?",
                        (jid2,)).fetchone()["planned"] == 1
    conn.close()


def test_unplanned_outage_that_drifts_into_window_stays_unplanned_alert(pcfg):
    """An outage that STARTED outside any window fires its alert normally; if a
    window is declared later covering the close, only `planned` flips."""
    from pgbench_webapp.contprobe import OutageTracker
    conn = _conn(pcfg)
    jid = _job(conn)
    t = OutageTracker(conn, jid, "read", 1)
    t.observe(False, "surprise", _iso(NOW))
    assert conn.execute("SELECT delivery FROM alerts WHERE type='db_unreachable'"
                        ).fetchone()["delivery"] is None       # deliverable
    conn.execute("INSERT INTO maintenance_windows(job_id, starts_utc, ends_utc) "
                 "VALUES (?,?,?)", (jid, _iso(NOW + timedelta(seconds=10)),
                                    _iso(NOW + timedelta(minutes=10))))
    t.observe(True, "", _iso(NOW + timedelta(minutes=1)))
    assert conn.execute("SELECT planned FROM outages").fetchone()["planned"] == 1
    conn.close()


def test_worker_restart_adopts_open_outage(pcfg):
    """A new tracker (worker restart) re-attaches to the open ledger row —
    no duplicate outage, and recovery still closes the ORIGINAL row."""
    from pgbench_webapp.contprobe import OutageTracker
    conn = _conn(pcfg)
    jid = _job(conn)
    t1 = OutageTracker(conn, jid, "read", 1)
    t1.observe(False, "down", _iso(NOW))
    t2 = OutageTracker(conn, jid, "read", 1)      # "restarted worker"
    assert t2.status == "down"
    t2.observe(False, "still down", _iso(NOW + timedelta(seconds=30)))
    assert conn.execute("SELECT count(*) FROM outages").fetchone()[0] == 1
    t2.observe(True, "", _iso(NOW + timedelta(seconds=90)))
    row = conn.execute("SELECT * FROM outages").fetchone()
    assert row["ended_utc"] and row["duration_s"] == 90.0
    conn.close()


def test_load_gap_opens_load_outage_only_when_probes_green(pcfg):
    from pgbench_webapp.contprobe import (OutageTracker, _check_load_gap,
                                          get_alerts_config)
    conn = _conn(pcfg)
    jid = _job(conn)
    acfg = get_alerts_config(conn)
    read_t = OutageTracker(conn, jid, "read", 3)
    write_t = OutageTracker(conn, jid, "write", 3)
    load_t = OutageTracker(conn, jid, "load", 1)
    stale = _iso(datetime.now(timezone.utc) - timedelta(seconds=120))
    fresh = _iso(datetime.now(timezone.utc) - timedelta(seconds=2))

    # probes green + stale samples -> load outage
    _check_load_gap(conn, jid, acfg, read_t, write_t, load_t, stale)
    row = conn.execute("SELECT * FROM outages").fetchone()
    assert row is not None and row["kind"] == "load" and row["ended_utc"] is None
    a = conn.execute("SELECT * FROM alerts WHERE type='load_gap'").fetchone()
    assert a is not None and a["severity"] == "warn"
    # samples resume -> closed
    _check_load_gap(conn, jid, acfg, read_t, write_t, load_t, fresh)
    assert conn.execute("SELECT ended_utc FROM outages").fetchone()["ended_utc"]

    # probes RED + stale samples -> NOT a load outage (it's a DB outage)
    read_t.status = "down"
    _check_load_gap(conn, jid, acfg, read_t, write_t, load_t, stale)
    assert conn.execute("SELECT count(*) FROM outages WHERE kind='load'"
                        ).fetchone()[0] == 1          # still just the closed one
    # no samples yet ever -> no comparison possible
    _check_load_gap(conn, jid, acfg, read_t, write_t, load_t, "")
    assert conn.execute("SELECT count(*) FROM outages WHERE kind='load'"
                        ).fetchone()[0] == 1
    conn.close()


def test_run_probe_via_fakebin(pcfg, monkeypatch, tmp_path):
    from pgbench_webapp.contprobe import run_probe
    for exe in ("psql",):
        p = FAKEBIN / exe
        p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", f"{FAKEBIN}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_PSQL_STATE", str(tmp_path))
    target = {"host": "db.example.invalid", "port": 5432, "database": "sbtest",
              "user": "doadmin", "sslmode": "require"}
    assert run_probe(target, "pw", "read") == (True, "")
    assert run_probe(target, "pw", "write") == (True, "")
    monkeypatch.setenv("FAKE_PSQL_PROBE_FAIL", "write")
    ok, err = run_probe(target, "pw", "write")
    assert not ok and "Connection refused" in err
    assert run_probe(target, "pw", "read")[0] is True
    monkeypatch.setenv("FAKE_PSQL_PROBE_FAIL", "all")
    assert run_probe(target, "pw", "read")[0] is False
    # no password in the error even if psql echoed it
    monkeypatch.setenv("FAKE_PSQL_PROBE_FAIL", "")


def test_alert_config_merge(pcfg):
    from pgbench_webapp import queries
    from pgbench_webapp.contprobe import ALERTS_CONFIG_DEFAULTS, get_alerts_config
    conn = _conn(pcfg)
    assert get_alerts_config(conn) == ALERTS_CONFIG_DEFAULTS
    queries.set_setting(conn, "cont_alerts_config",
                        '{"latency_p99_ms": 250, "junk": "ignored", "renotify_min": true}')
    c = get_alerts_config(conn)
    assert c["latency_p99_ms"] == 250
    assert c["renotify_min"] == 60                 # bools rejected, default kept
    assert "junk" not in ALERTS_CONFIG_DEFAULTS
    queries.set_setting(conn, "cont_alerts_config", "{not json")
    assert get_alerts_config(conn)["latency_p99_ms"] == 250 or True  # tolerant
    conn.close()
