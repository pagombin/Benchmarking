"""Alert engine: dedup, resolution, renotify cadence, delivery retry/backoff
bookkeeping, store-first-even-when-Slack-fails, condition rules, collector."""

from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

FAKEBIN = Path(__file__).resolve().parent / "fakebin"


@pytest.fixture()
def acfg_env(tmp_path, monkeypatch):
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


def _store(cfg):
    from pgbench_webapp.secrets_store import SecretStore
    return SecretStore(cfg.secret_key_path, cfg.data_dir / "secrets.enc")


def _slack_on(conn, store):
    from pgbench_webapp import notify, queries
    queries.set_setting(conn, "notify_config", json.dumps({"slack": {"enabled": True}}))
    store.set(notify.SLACK_WEBHOOK_REF, "https://hooks.slack.example/T000/B000/x")


def _job(conn, state="running") -> int:
    from pgbench_webapp import queries
    jid = queries.enqueue_job(conn, "continuous", "spec: {}", None, "t",
                              desired_state="running")
    queries.update_job(conn, jid, state=state,
                       started_utc="2020-01-01T00:00:00Z", run_id=f"r{jid}")
    return jid


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ── storage semantics ───────────────────────────────────────────────

def test_dedup_while_unresolved_then_refire_after_resolve(acfg_env):
    from pgbench_webapp import alerts
    conn = _conn(acfg_env)
    a = alerts.store_alert(conn, type="latency", severity="warn", dedup_key="k1")
    assert a is not None
    assert alerts.store_alert(conn, type="latency", severity="warn",
                              dedup_key="k1") is None          # deduped
    assert conn.execute("SELECT count(*) FROM alerts").fetchone()[0] == 1
    assert alerts.resolve_alert(conn, "k1") == 1
    assert alerts.store_alert(conn, type="latency", severity="warn",
                              dedup_key="k1") is not None      # a NEW incident
    assert conn.execute("SELECT count(*) FROM alerts").fetchone()[0] == 2
    conn.close()


def test_point_alerts_never_block(acfg_env):
    from pgbench_webapp import alerts
    conn = _conn(acfg_env)
    for _ in range(3):
        assert alerts.store_alert(conn, type="harness_relaunch", severity="info",
                                  dedup_key="p1", point=True) is not None
    rows = list(conn.execute("SELECT * FROM alerts"))
    assert len(rows) == 3 and all(r["resolved_utc"] for r in rows)
    conn.close()


# ── delivery ────────────────────────────────────────────────────────

def test_delivery_retry_backoff_recorded(acfg_env):
    from pgbench_webapp import alerts
    cfg = acfg_env
    conn = _conn(cfg)
    store = _store(cfg)
    _slack_on(conn, store)
    jid = _job(conn)
    alerts.store_alert(conn, type="db_unreachable", severity="crit",
                       dedup_key=f"job:{jid}:x", job_id=jid,
                       context={"kind": "read"})
    calls = {"n": 0}

    def flaky(_hook, _text):
        calls["n"] += 1
        if calls["n"] < 3:
            raise OSError("slack 502")
        return True

    assert alerts.deliver_pending(conn, store, send=flaky, base_delay_s=0.001) == 1
    row = conn.execute("SELECT * FROM alerts").fetchone()
    assert row["delivery"] == alerts.DELIVERY_OK
    assert row["delivery_attempts"] == 3
    assert row["delivered_utc"] and row["last_notified_utc"]
    conn.close()


def test_alert_stored_even_when_slack_always_fails(acfg_env):
    from pgbench_webapp import alerts
    cfg = acfg_env
    conn = _conn(cfg)
    store = _store(cfg)
    _slack_on(conn, store)
    alerts.store_alert(conn, type="no_data", severity="crit", dedup_key="k")

    def dead(_hook, _text):
        raise OSError("slack down")

    alerts.deliver_pending(conn, store, send=dead, base_delay_s=0.001)
    row = conn.execute("SELECT * FROM alerts").fetchone()
    assert row is not None                              # the ledger kept it
    assert row["delivery"] == alerts.DELIVERY_FAILED
    assert row["delivery_attempts"] == 5                # all five tries recorded
    assert row["delivered_utc"] is None
    conn.close()


def test_delivery_unconfigured_marks_none(acfg_env):
    from pgbench_webapp import alerts
    cfg = acfg_env
    conn = _conn(cfg)
    alerts.store_alert(conn, type="latency", severity="warn", dedup_key="k")
    alerts.deliver_pending(conn, _store(cfg),
                           send=lambda *_: (_ for _ in ()).throw(AssertionError))
    assert conn.execute("SELECT delivery FROM alerts").fetchone()[0] == \
        alerts.DELIVERY_NONE
    conn.close()


def test_suppressed_rows_are_never_delivered(acfg_env):
    from pgbench_webapp import alerts
    cfg = acfg_env
    conn = _conn(cfg)
    store = _store(cfg)
    _slack_on(conn, store)
    alerts.store_alert(conn, type="db_unreachable", severity="crit",
                       dedup_key="k", suppressed=True)
    sent = []
    alerts.deliver_pending(conn, store, send=lambda *_a: sent.append(1) or True)
    assert not sent
    assert conn.execute("SELECT delivery FROM alerts").fetchone()[0] == \
        alerts.DELIVERY_SUPPRESSED
    conn.close()


def test_renotify_cadence_for_standing_crits(acfg_env):
    from pgbench_webapp import alerts
    cfg = acfg_env
    conn = _conn(cfg)
    store = _store(cfg)
    _slack_on(conn, store)
    jid = _job(conn)
    alerts.store_alert(conn, type="db_unreachable", severity="crit",
                       dedup_key=f"job:{jid}:d", job_id=jid)
    sent: list[str] = []
    alerts.deliver_pending(conn, store, send=lambda _h, t: sent.append(t) or True)
    assert len(sent) == 1
    # fresh delivery -> no renotify yet
    assert alerts.renotify_crits(conn, store, renotify_min=60,
                                 send=lambda _h, t: sent.append(t) or True) == 0
    # age the last notification past the cadence -> exactly one "still firing"
    old = _iso(datetime.now(timezone.utc) - timedelta(minutes=61))
    conn.execute("UPDATE alerts SET last_notified_utc=?", (old,))
    assert alerts.renotify_crits(conn, store, renotify_min=60,
                                 send=lambda _h, t: sent.append(t) or True) == 1
    assert "still firing" in sent[-1]
    assert alerts.renotify_crits(conn, store, renotify_min=60,
                                 send=lambda _h, t: sent.append(t) or True) == 0
    # resolved crits are never re-notified
    conn.execute("UPDATE alerts SET last_notified_utc=?, resolved_utc=?", (old, old))
    assert alerts.renotify_crits(conn, store, renotify_min=60,
                                 send=lambda _h, t: sent.append(t) or True) == 0
    # warn alerts are never re-notified either
    alerts.store_alert(conn, type="latency", severity="warn", dedup_key="w")
    conn.execute("UPDATE alerts SET last_notified_utc=?, delivery=? "
                 "WHERE dedup_key='w'", (old, alerts.DELIVERY_OK))
    assert alerts.renotify_crits(conn, store, renotify_min=60,
                                 send=lambda _h, t: sent.append(t) or True) == 0
    conn.close()


def test_slack_text_shape(acfg_env):
    from pgbench_webapp import alerts, queries
    cfg = acfg_env
    conn = _conn(cfg)
    tid = queries.create_target(conn, "prod-adv-nyc3", "h", 5432, "db", "u",
                                "require", "ref")
    jid = _job(conn)
    queries.update_job(conn, jid, target_id=tid)
    alerts.store_alert(conn, type="db_unreachable", severity="crit",
                       dedup_key="k", job_id=jid,
                       context={"kind": "write", "first_error": "conn refused"})
    row = conn.execute("SELECT * FROM alerts").fetchone()
    text = alerts.slack_text(conn, row, base_url="https://console.example:8443")
    assert "🔴" in text and "prod-adv-nyc3" in text and "db_unreachable" in text
    assert "write probe" in text and "conn refused" in text
    assert f"/ui/continuous/{jid}?window=1h" in text
    conn.close()


# ── engine conditions ───────────────────────────────────────────────

def _seed_samples(conn, jid, *, seconds, err=0.0, lat=45.0, tps=100.0,
                  end=None):
    end = end or datetime.now(timezone.utc)
    rows = []
    for i in range(seconds):
        ts = _iso(end - timedelta(seconds=i))
        rows.append((jid, ts, tps, tps * 20, lat, err, 0.0))
    conn.executemany(
        "INSERT OR REPLACE INTO cont_samples(job_id, ts_utc, tps, qps, lat_p99, "
        "err_s, reconn_s) VALUES (?,?,?,?,?,?,?)", rows)


def test_error_rate_and_latency_conditions(acfg_env):
    from pgbench_webapp import contworker
    from pgbench_webapp.contprobe import get_alerts_config
    from pgbench_webapp import queries
    cfg = acfg_env
    conn = _conn(cfg)
    jid = _job(conn)
    job = queries.get_job(conn, jid)
    ac = get_alerts_config(conn)

    _seed_samples(conn, jid, seconds=70, err=5.0, lat=800.0)   # both breaching
    contworker.evaluate_job_conditions(cfg, conn, job, ac)
    types = {r["type"]: r for r in conn.execute("SELECT * FROM alerts")}
    assert "error_rate" in types and types["error_rate"]["severity"] == "warn"
    assert "latency" not in types          # 300s hold not yet satisfied (70s of data)

    _seed_samples(conn, jid, seconds=310, err=5.0, lat=800.0)
    contworker.evaluate_job_conditions(cfg, conn, job, ac)
    types = {r["type"] for r in conn.execute("SELECT * FROM alerts")}
    assert "latency" in types

    # healthy again -> both resolve
    _seed_samples(conn, jid, seconds=310, err=0.0, lat=40.0)
    contworker.evaluate_job_conditions(cfg, conn, job, ac)
    open_types = {r["type"] for r in conn.execute(
        "SELECT type FROM alerts WHERE resolved_utc IS NULL")}
    assert "error_rate" not in open_types and "latency" not in open_types
    conn.close()


def test_tps_drop_needs_24h_history(acfg_env):
    from pgbench_webapp import contworker, queries
    from pgbench_webapp.contprobe import get_alerts_config
    cfg = acfg_env
    conn = _conn(cfg)
    jid = _job(conn)
    job = queries.get_job(conn, jid)
    ac = get_alerts_config(conn)
    now = datetime.now(timezone.utc)
    _seed_samples(conn, jid, seconds=310, tps=30.0)            # collapsed load
    contworker.evaluate_job_conditions(cfg, conn, job, ac)
    assert conn.execute("SELECT count(*) FROM alerts WHERE type='tps_drop'"
                        ).fetchone()[0] == 0                    # no baseline yet
    # seed 24h of minute rollups with median tps 100
    rows = [(jid, _iso(now - timedelta(minutes=i))[:17] + "00Z", 60, 100.0, 0)
            for i in range(1, 1441)]
    conn.executemany("INSERT OR REPLACE INTO cont_rollup_1m(job_id, ts_utc, n, "
                     "tps_avg, gap_s) VALUES (?,?,?,?,?)", rows)
    contworker.evaluate_job_conditions(cfg, conn, job, ac)
    row = conn.execute("SELECT * FROM alerts WHERE type='tps_drop'").fetchone()
    assert row is not None and row["severity"] == "warn"
    # recovery resolves it
    _seed_samples(conn, jid, seconds=310, tps=100.0)
    contworker.evaluate_job_conditions(cfg, conn, job, ac)
    assert conn.execute("SELECT resolved_utc FROM alerts WHERE type='tps_drop'"
                        ).fetchone()["resolved_utc"]
    conn.close()


def test_auth_failure_condition_from_state_json(acfg_env):
    from pgbench_webapp import contworker, queries
    from pgbench_webapp.contprobe import get_alerts_config
    cfg = acfg_env
    conn = _conn(cfg)
    jid = _job(conn)
    job = queries.get_job(conn, jid)
    run_dir = cfg.results_dir / str(job["run_id"])
    run_dir.mkdir(parents=True)
    (run_dir / "state.json").write_text(json.dumps(
        {"status": "backoff", "last_error_class": "auth",
         "last_error": "password authentication failed"}))
    contworker.evaluate_job_conditions(cfg, conn, job, get_alerts_config(conn))
    row = conn.execute("SELECT * FROM alerts WHERE type='auth_failure'").fetchone()
    assert row is not None and row["severity"] == "crit"
    # supervisor recovers -> resolved
    (run_dir / "state.json").write_text(json.dumps(
        {"status": "running", "last_error_class": ""}))
    contworker.evaluate_job_conditions(cfg, conn, job, get_alerts_config(conn))
    assert conn.execute("SELECT resolved_utc FROM alerts WHERE type='auth_failure'"
                        ).fetchone()["resolved_utc"]
    conn.close()


def test_no_data_condition(acfg_env, monkeypatch):
    import time as _t
    from pgbench_webapp import contworker, queries
    from pgbench_webapp import contprobe
    from pgbench_webapp.contprobe import get_alerts_config
    cfg = acfg_env
    conn = _conn(cfg)
    jid = _job(conn)                        # started 2020, no samples, no probes
    job = queries.get_job(conn, jid)
    # a freshly-booted worker gets a grace period: no firing yet
    monkeypatch.setattr(contworker, "_ENGINE_EPOCH", _t.monotonic())
    contworker.evaluate_job_conditions(cfg, conn, job, get_alerts_config(conn))
    assert conn.execute("SELECT count(*) FROM alerts WHERE type='no_data'"
                        ).fetchone()[0] == 0
    # past the grace, a silent pipeline is a crit
    monkeypatch.setattr(contworker, "_ENGINE_EPOCH", _t.monotonic() - 10000)
    contworker.evaluate_job_conditions(cfg, conn, job, get_alerts_config(conn))
    assert conn.execute("SELECT count(*) FROM alerts WHERE type='no_data'"
                        ).fetchone()[0] == 1
    # a live probe tick resolves it
    import time as _time
    contprobe.LAST_PROBE_TICK[jid] = _time.monotonic()
    contworker.evaluate_job_conditions(cfg, conn, job, get_alerts_config(conn))
    assert conn.execute("SELECT resolved_utc FROM alerts WHERE type='no_data'"
                        ).fetchone()["resolved_utc"]
    contprobe.LAST_PROBE_TICK.pop(jid, None)
    conn.close()


def test_loadgen_disk_condition_with_hysteresis(acfg_env, monkeypatch):
    import shutil
    from collections import namedtuple
    from pgbench_webapp import contworker
    from pgbench_webapp.contprobe import get_alerts_config
    cfg = acfg_env
    conn = _conn(cfg)
    DU = namedtuple("DU", "total used free")
    monkeypatch.setattr(shutil, "disk_usage", lambda _p: DU(100, 90, 10))
    contworker.check_loadgen_disk(cfg, conn, get_alerts_config(conn))
    row = conn.execute("SELECT * FROM alerts WHERE type='loadgen_disk'").fetchone()
    assert row is not None and row["job_id"] is None
    # 82% is inside the hysteresis band: still firing
    monkeypatch.setattr(shutil, "disk_usage", lambda _p: DU(100, 82, 18))
    contworker.check_loadgen_disk(cfg, conn, get_alerts_config(conn))
    assert conn.execute("SELECT resolved_utc FROM alerts WHERE type='loadgen_disk'"
                        ).fetchone()["resolved_utc"] is None
    monkeypatch.setattr(shutil, "disk_usage", lambda _p: DU(100, 50, 50))
    contworker.check_loadgen_disk(cfg, conn, get_alerts_config(conn))
    assert conn.execute("SELECT resolved_utc FROM alerts WHERE type='loadgen_disk'"
                        ).fetchone()["resolved_utc"]
    conn.close()


def test_maintenance_suppresses_engine_conditions(acfg_env):
    from pgbench_webapp import alerts, contworker, queries
    from pgbench_webapp.contprobe import get_alerts_config
    cfg = acfg_env
    conn = _conn(cfg)
    jid = _job(conn)
    now = datetime.now(timezone.utc)
    conn.execute("INSERT INTO maintenance_windows(job_id, starts_utc, ends_utc) "
                 "VALUES (?,?,?)",
                 (jid, _iso(now - timedelta(hours=1)), _iso(now + timedelta(hours=1))))
    _seed_samples(conn, jid, seconds=70, err=9.0)
    contworker.evaluate_job_conditions(cfg, conn, queries.get_job(conn, jid),
                                       get_alerts_config(conn))
    row = conn.execute("SELECT * FROM alerts WHERE type='error_rate'").fetchone()
    assert row is not None                                  # stored in the ledger
    assert row["delivery"] == alerts.DELIVERY_SUPPRESSED   # but never sent
    conn.close()


# ── notify test endpoint helper + collector ─────────────────────────

def test_notify_test_reports_per_channel(acfg_env, monkeypatch):
    from pgbench_webapp import notify, queries
    cfg = acfg_env
    conn = _conn(cfg)
    store = _store(cfg)
    out = notify.notify_test(conn, store)
    assert out["slack"]["configured"] is False and out["email"]["configured"] is False
    _slack_on(conn, store)
    monkeypatch.setattr(notify, "_send_slack", lambda *_a: True)
    out = notify.notify_test(conn, store, text="custom check 123")
    assert out["slack"] == {"configured": True, "ok": True, "error": ""}

    def boom(hook, _t):
        raise OSError(f"cannot reach {hook}")
    monkeypatch.setattr(notify, "_send_slack", boom)
    out = notify.notify_test(conn, store)
    assert out["slack"]["ok"] is False
    assert "***" in out["slack"]["error"]          # webhook never echoed back
    assert "hooks.slack.example" not in out["slack"]["error"]
    conn.close()


def test_collector_stores_raw_counters(acfg_env, monkeypatch, tmp_path):
    from pgbench_webapp import contcollect, queries
    for exe in ("psql",):
        p = FAKEBIN / exe
        p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", f"{FAKEBIN}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_PSQL_STATE", str(tmp_path / "st"))
    (tmp_path / "st").mkdir()
    monkeypatch.setenv("FAKE_PSQL_PGSS", "1")
    cfg = acfg_env
    conn = _conn(cfg)
    store = _store(cfg)
    store.set("target:t1:password", "pw")
    tid = queries.create_target(conn, "t1", "db.example.invalid", 5432, "sbtest",
                                "doadmin", "require", "target:t1:password")
    jid = _job(conn)
    conn.execute("UPDATE jobs SET target_id=?, spec_yaml=? WHERE id=?",
                 (tid, "target:\n  host: db.example.invalid\n", jid))
    assert contcollect.collector_tick(cfg, conn, store) == 1
    row = conn.execute("SELECT * FROM cont_db_metrics WHERE job_id=?", (jid,)).fetchone()
    m = json.loads(row["metrics"])
    assert m["conn_active"] == 3 and m["conn_total"] == 9
    assert m["xact_commit"] > 0 and m["wal_bytes"] > 0
    assert m["ckpt_timed"] == 12 and m["archive_failed"] == 0
    assert m["db_size"] == 1073741824 and m["dead_tup"] == 1234
    assert m["top_queries"][0]["calls"] == 50000
    # interval gate: a second immediate tick stores nothing
    assert contcollect.collector_tick(cfg, conn, store) == 0
    conn.close()


def test_dead_man_heartbeat_gating(acfg_env, monkeypatch):
    import urllib.request
    from pgbench_webapp import contworker, queries
    cfg = acfg_env
    conn = _conn(cfg)
    hits: list[str] = []

    class _Resp:
        def close(self):
            pass

    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda url, timeout=5: hits.append(url) or _Resp())
    contworker._last_beat = 0.0
    # unset URL -> dormant
    assert contworker.dead_man_heartbeat(cfg, conn) is False
    queries.set_setting(conn, "heartbeat_url", "https://hc-ping.example/uuid")
    # no desired-running continuous job -> no beat
    assert contworker.dead_man_heartbeat(cfg, conn) is False
    _job(conn)
    assert contworker.dead_man_heartbeat(cfg, conn) is True
    assert hits == ["https://hc-ping.example/uuid"]
    # cadence: immediate second call is a no-op
    assert contworker.dead_man_heartbeat(cfg, conn) is False
    # non-http URL is refused
    contworker._last_beat = 0.0
    queries.set_setting(conn, "heartbeat_url", "file:///etc/passwd")
    assert contworker.dead_man_heartbeat(cfg, conn) is False
    assert len(hits) == 1
    conn.close()
