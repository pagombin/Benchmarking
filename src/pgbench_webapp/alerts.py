"""Alert history: store-first semantics over the ``alerts`` table.

Every alert is STORED before any delivery is attempted (the ledger is the
truth; Slack is a best-effort mirror). Deduplication is by ``dedup_key``: while
an unresolved row with the same key exists, the same condition does not re-fire
a new row. Point events (informational one-shots like ``harness_relaunch``)
are stored pre-resolved so they never block a future occurrence.

Delivery is decoupled: rows are inserted with ``delivery IS NULL`` and the
worker's alert engine (see ``contworker``) picks them up, attempts Slack with
retry/backoff, and records the outcome on the row. Nothing in here raises for
operational reasons — a failed insert must never break the caller's real work.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Optional

from pgbench_webapp.util import utc_now_iso

SEVERITIES = ("info", "warn", "crit")

# delivery column values
DELIVERY_PENDING = None          # not yet attempted (engine picks these up)
DELIVERY_OK = "slack:ok"
DELIVERY_FAILED = "slack:failed"
DELIVERY_NONE = "none"           # no channel configured
DELIVERY_SUPPRESSED = "suppressed"   # maintenance window — stored, not sent


def open_alert(conn: sqlite3.Connection, dedup_key: str) -> Optional[sqlite3.Row]:
    """The unresolved alert row for *dedup_key*, if one exists."""
    return conn.execute(
        "SELECT * FROM alerts WHERE dedup_key=? AND resolved_utc IS NULL "
        "ORDER BY id DESC LIMIT 1", (dedup_key,)).fetchone()


def store_alert(conn: sqlite3.Connection, *, type: str, severity: str,
                dedup_key: str, job_id: Optional[int] = None,
                context: Optional[dict[str, Any]] = None,
                point: bool = False,
                suppressed: bool = False) -> Optional[int]:
    """Insert an alert row unless an unresolved one with the same dedup_key
    exists. Returns the new row id, or None when deduplicated.

    ``point=True`` stores the row already resolved (a one-shot informational
    event, not an ongoing condition). ``suppressed=True`` marks the row as
    matched by a maintenance window: kept in the ledger, never delivered.
    """
    if severity not in SEVERITIES:
        severity = "info"
    if not point and open_alert(conn, dedup_key) is not None:
        return None
    now = utc_now_iso()
    cur = conn.execute(
        "INSERT INTO alerts(job_id, type, severity, fired_utc, resolved_utc, "
        "dedup_key, context, delivery, delivery_attempts) "
        "VALUES (?,?,?,?,?,?,?,?,0)",
        (job_id, type, severity, now, now if point else None, dedup_key,
         json.dumps(context or {}),
         DELIVERY_SUPPRESSED if suppressed else DELIVERY_PENDING))
    return int(cur.lastrowid or 0)


def resolve_alert(conn: sqlite3.Connection, dedup_key: str) -> int:
    """Mark all unresolved rows for *dedup_key* resolved; returns count."""
    cur = conn.execute(
        "UPDATE alerts SET resolved_utc=? WHERE dedup_key=? AND resolved_utc IS NULL",
        (utc_now_iso(), dedup_key))
    return int(cur.rowcount or 0)


def record_delivery(conn: sqlite3.Connection, alert_id: int, delivery: str,
                    attempts: int, delivered: bool) -> None:
    now = utc_now_iso()
    # last_notified_utc records the delivery ATTEMPT time (ok or failed), so a
    # crit whose first Slack push failed is still retried on the renotify
    # cadence rather than lost forever.
    conn.execute(
        "UPDATE alerts SET delivery=?, delivery_attempts=?, delivered_utc=?, "
        "last_notified_utc=? WHERE id=?",
        (delivery, attempts, now if delivered else None,
         now if delivery != DELIVERY_SUPPRESSED else None, alert_id))


def pending_deliveries(conn: sqlite3.Connection, limit: int = 20) -> list[sqlite3.Row]:
    """Alert rows stored but not yet delivery-attempted, oldest first."""
    return list(conn.execute(
        "SELECT * FROM alerts WHERE delivery IS NULL ORDER BY id LIMIT ?",
        (limit,)))


def mark_renotified(conn: sqlite3.Connection, alert_id: int) -> None:
    conn.execute("UPDATE alerts SET last_notified_utc=? WHERE id=?",
                 (utc_now_iso(), alert_id))


# ── Slack delivery (retry + backoff over notify's primitive) ────────

SEVERITY_EMOJI = {"info": "ℹ️", "warn": "⚠️", "crit": "🔴"}

# alert type -> one-line human template (context keys are best-effort)
_TYPE_LINE = {
    "db_unreachable": "database unreachable ({kind} probe down after retries)",
    "db_recovered": "database recovered ({kind} probe; outage {duration_s:.0f}s)",
    "load_gap": "load generator stalled (no samples while probes are green)",
    "load_resumed": "load generator resumed (stall {duration_s:.0f}s)",
    "error_rate": "sustained SQL error rate",
    "latency": "sustained p99 latency above threshold",
    "tps_drop": "throughput drop vs the trailing 24h median",
    "auth_failure": "authentication failing — relaunches held at max backoff",
    "harness_relaunch": "harness relaunched (reboot/crash recovery)",
    "loadgen_disk": "load-generator data volume filling up",
    "no_data": "no samples AND no probe results — pipeline wedged",
}


def _job_target_name(conn: sqlite3.Connection, job_id: Optional[int]) -> str:
    if job_id is None:
        return "loadgen host"
    row = conn.execute(
        "SELECT t.name AS name, t.host AS host FROM jobs j "
        "LEFT JOIN targets t ON t.id = j.target_id WHERE j.id=?",
        (job_id,)).fetchone()
    if row is None:
        return f"job {job_id}"
    return str(row["name"] or row["host"] or f"job {job_id}")


def slack_text(conn: sqlite3.Connection, row: sqlite3.Row,
               base_url: str = "", still_firing: bool = False) -> str:
    """One Slack message for one alert row: severity emoji, target, type,
    concise detail, console deep link into the continuous view."""
    try:
        ctx = json.loads(row["context"] or "{}")
    except ValueError:
        ctx = {}
    line = _TYPE_LINE.get(row["type"], row["type"])
    try:
        detail = line.format(**{"kind": ctx.get("kind", "?"),
                                "duration_s": float(ctx.get("duration_s", 0) or 0)})
    except (KeyError, ValueError, IndexError):
        detail = line
    target = _job_target_name(conn, row["job_id"])
    head = (f"{SEVERITY_EMOJI.get(row['severity'], '•')} "
            f"[pgbench continuous] {target} — {row['type']}"
            + (" (still firing)" if still_firing else ""))
    parts = [f"*{head}*", detail]
    extra = ctx.get("first_error") or ctx.get("reason") or ctx.get("detail") or ""
    if extra:
        parts.append(str(extra)[:300])
    parts.append(f"fired {row['fired_utc']}")
    if base_url and row["job_id"] is not None:
        parts.append(f"{base_url.rstrip('/')}/ui/continuous/{row['job_id']}?window=1h")
    return "\n".join(parts)


def send_with_retry(send: Any, webhook: str, text: str, attempts: int = 5,
                    base_delay_s: float = 0.5) -> tuple[bool, int]:
    """Up to *attempts* tries with exponential backoff + jitter. Never raises.
    Returns (delivered, attempts_made)."""
    import random
    import time as _time
    for i in range(1, max(1, attempts) + 1):
        try:
            if send(webhook, text):
                return True, i
            return False, i          # not configured — no point retrying
        except Exception:  # noqa: BLE001 — delivery must never raise
            if i >= attempts:
                return False, i
            delay = base_delay_s * (2 ** (i - 1))
            _time.sleep(random.uniform(0, delay))
    return False, attempts


def deliver_pending(conn: sqlite3.Connection, store: Any, *,
                    attempts: int = 5, base_delay_s: float = 0.5,
                    send: Any = None, limit: int = 20) -> int:
    """Attempt Slack delivery for stored-but-unattempted alert rows.

    Store-first is already guaranteed (the rows exist); this only mirrors them
    out. With Slack unconfigured the rows are marked 'none' so they never pend
    forever. Returns the number of rows attempted.
    """
    from pgbench_webapp import notify, queries
    rows = pending_deliveries(conn, limit=limit)
    if not rows:
        return 0
    if send is None:
        send = notify._send_slack
    cfg = notify.get_config(conn)
    webhook = store.get(notify.SLACK_WEBHOOK_REF)
    enabled = bool((cfg.get("slack") or {}).get("enabled")) and bool(webhook)
    base_url = queries.get_setting(conn, "base_url", "")
    n = 0
    for row in rows:
        n += 1
        if not enabled:
            record_delivery(conn, int(row["id"]), DELIVERY_NONE, 0, False)
            continue
        ok, tries = send_with_retry(send, webhook or "",
                                    slack_text(conn, row, base_url),
                                    attempts=attempts, base_delay_s=base_delay_s)
        record_delivery(conn, int(row["id"]),
                        DELIVERY_OK if ok else DELIVERY_FAILED, tries, ok)
    return n


def renotify_crits(conn: sqlite3.Connection, store: Any, *,
                   renotify_min: int = 60, attempts: int = 3,
                   base_delay_s: float = 0.5, send: Any = None) -> int:
    """Re-send "still firing" for unresolved crit alerts every renotify_min."""
    from datetime import datetime, timedelta, timezone
    from pgbench_webapp import notify, queries
    cfg = notify.get_config(conn)
    webhook = store.get(notify.SLACK_WEBHOOK_REF)
    if not ((cfg.get("slack") or {}).get("enabled") and webhook):
        return 0
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=max(1, renotify_min))) \
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = list(conn.execute(
        "SELECT * FROM alerts WHERE resolved_utc IS NULL AND severity='crit' "
        "AND delivery IN (?, ?) AND last_notified_utc IS NOT NULL "
        "AND last_notified_utc < ?",
        (DELIVERY_OK, DELIVERY_FAILED, cutoff)))
    base_url = queries.get_setting(conn, "base_url", "")
    if send is None:
        send = notify._send_slack
    n = 0
    for row in rows:
        send_with_retry(send, webhook, slack_text(conn, row, base_url,
                                                  still_firing=True),
                        attempts=attempts, base_delay_s=base_delay_s)
        mark_renotified(conn, int(row["id"]))
        n += 1
    return n


def list_alerts(conn: sqlite3.Connection, *, job_id: Optional[int] = None,
                type: str = "", severity: str = "", since_utc: str = "",
                unresolved_only: bool = False,
                limit: int = 500, offset: int = 0) -> list[sqlite3.Row]:
    where, params = ["1=1"], []      # type: ignore[var-annotated]
    if job_id is not None:
        where.append("job_id=?")
        params.append(job_id)
    if type:
        where.append("type=?")
        params.append(type)
    if severity:
        where.append("severity=?")
        params.append(severity)
    if since_utc:
        where.append("fired_utc >= ?")
        params.append(since_utc)
    if unresolved_only:
        where.append("resolved_utc IS NULL")
    return list(conn.execute(
        f"SELECT * FROM alerts WHERE {' AND '.join(where)} "
        "ORDER BY id DESC LIMIT ? OFFSET ?", (*params, limit, offset)))
