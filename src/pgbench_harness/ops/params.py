"""Live PostgreSQL parameter catalog — introspected, never hand-typed.

``ops pg-params`` snapshots the FULL ``pg_settings`` catalog from the current
Patroni leader: every GUC the running server actually has (version- and
extension-accurate), with its live value, type, unit, min/max, enum values,
context (how a change is applied) and description. The webapp caches the
snapshot onto the Kube Target so the console can offer a searchable,
click-to-apply parameter map whose validation (types, ranges, enums) comes
from the server itself — a wrong name or out-of-range value is impossible to
stage.

A static overlay classifies each parameter's APPLY CHANNEL, because on a
Patroni-managed cluster not everything may go through
``spec.patroni.dynamicConfiguration.postgresql.parameters``:

* ``patroni-locked``  — Patroni owns these and silently overrides user values
  (listen_addresses, port, hot_standby, ...): never apply via the CR.
* ``dcs-coordinated`` — Patroni validates/coordinates these cluster-wide
  through DCS and orders a coordinated restart when they shrink
  (max_connections, wal_level, ...): applying works but expect
  pending_restart + a rolling restart.
* ``cr``              — normal: patch the CR, operator syncs DCS, Patroni
  reloads; postmaster-context ones still need a restart.
* ``readonly``        — pg_settings context 'internal': compiled in, display
  only.
"""

from __future__ import annotations

import json
from typing import Any

from pgbench_harness.ops.crconfig import PATRONI_PARAMS_PATH, PGBACKREST_GLOBAL_PATH, _dig
from pgbench_harness.ops.kube import Kube, KubeError
from pgbench_harness.ops.opspec import OpsSpec
from pgbench_harness.util import utc_now_iso

PARAMS_MARKER = "OPS_PARAMS_JSON"

# Patroni force-sets these on every member; a CR-supplied value is ignored or
# actively overwritten. (Patroni docs: "PostgreSQL parameters controlled by
# Patroni".)
PATRONI_LOCKED = frozenset({
    "listen_addresses", "port", "cluster_name", "hot_standby", "wal_log_hints",
})

# Patroni accepts these only through DCS dynamic configuration and coordinates
# them cluster-wide (they must match on primary and replicas; several are
# restart-only and a decrease triggers a coordinated restart order).
PATRONI_DCS = frozenset({
    "max_connections", "max_locks_per_transaction", "max_worker_processes",
    "max_prepared_transactions", "max_replication_slots", "max_wal_senders",
    "wal_level", "wal_keep_segments", "wal_keep_size", "track_commit_timestamp",
})

# One JSON document out of psql: no separator/quoting pitfalls, enumvals stays
# a real array. ORDER BY inside the aggregate keeps the catalog stable.
_CATALOG_SQL = (
    "SELECT coalesce(json_agg(row_to_json(s) ORDER BY s.name), '[]'::json) FROM ("
    "SELECT name, setting, unit, vartype, min_val, max_val, enumvals, context, "
    "category, short_desc, boot_val, reset_val, source, pending_restart "
    "FROM pg_settings) s")


def _check(name: str, status: str, detail: str = "") -> None:
    print(json.dumps({"name": name, "status": status, "detail": detail}), flush=True)


def classify(name: str, context: str) -> str:
    """The apply channel for one parameter (see module docstring)."""
    if context == "internal":
        return "readonly"
    if name in PATRONI_LOCKED:
        return "patroni-locked"
    if name in PATRONI_DCS:
        return "dcs-coordinated"
    return "cr"


def build_catalog(rows: list[dict[str, Any]], cr_params: dict[str, Any]) -> list[dict[str, Any]]:
    """Merge live pg_settings rows with the channel overlay + CR-managed values."""
    out = []
    for r in rows:
        name = str(r.get("name", ""))
        context = str(r.get("context", ""))
        cr_val = cr_params.get(name)
        out.append({
            "name": name,
            "setting": r.get("setting"),
            "unit": r.get("unit"),
            "vartype": r.get("vartype"),
            "min_val": r.get("min_val"),
            "max_val": r.get("max_val"),
            "enumvals": r.get("enumvals") or [],
            "context": context,
            "category": r.get("category"),
            "short_desc": r.get("short_desc"),
            "boot_val": r.get("boot_val"),
            "reset_val": r.get("reset_val"),
            "source": r.get("source"),
            "pending_restart": bool(r.get("pending_restart")),
            "channel": classify(name, context),
            "restart_required": context == "postmaster",
            "cr_value": None if cr_val is None else str(cr_val),
        })
    return out


def run_pg_params(spec: OpsSpec) -> int:
    """Snapshot the parameter catalog from the leader. Exit 0 ok / 3 failed."""
    t = spec.target
    kube = Kube(context=t.context, namespace=t.namespace)
    payload: dict[str, Any] = {"collected_utc": utc_now_iso(), "leader": "",
                               "pg_version": "", "params": [],
                               "cr_managed": {}, "pgbackrest_global": {}}

    try:
        from pgbench_harness.ops.crconfig import resolve_leader
        _instances, leader, _view = resolve_leader(kube, t.cr_name)
        payload["leader"] = leader
        _check("leader", "ok", leader)
    except KubeError as exc:
        _check("leader", "fail", str(exc)[:300])
        print(f"{PARAMS_MARKER} {json.dumps(payload)}", flush=True)
        return 3

    cr_params: dict[str, Any] = {}
    try:
        cr = kube.cluster_cr(t.cr_kind, t.cr_name)
        cr_params = _dig(cr, PATRONI_PARAMS_PATH)
        payload["cr_managed"] = {k: str(v) for k, v in cr_params.items()}
        payload["pgbackrest_global"] = {
            k: str(v) for k, v in _dig(cr, PGBACKREST_GLOBAL_PATH).items()}
        _check("cluster-cr", "ok",
               f"{len(cr_params)} parameter(s) managed via the CR")
    except KubeError as exc:
        # Catalog still valuable without the CR overlay — degrade, don't die.
        _check("cluster-cr", "warn", str(exc)[:300])

    try:
        res = kube.psql(leader, _CATALOG_SQL, timeout_s=30)
        if not res.ok:
            raise KubeError((res.stderr or res.stdout).strip()[:300]
                            or "psql failed")
        rows = json.loads(res.stdout.strip() or "[]")
        if not isinstance(rows, list) or not rows:
            raise KubeError("pg_settings returned no rows")
    except (KubeError, ValueError) as exc:
        _check("pg-settings", "fail", str(exc)[:300])
        print(f"{PARAMS_MARKER} {json.dumps(payload)}", flush=True)
        return 3

    payload["params"] = build_catalog(rows, cr_params)
    ver = kube.psql(leader, "SHOW server_version")
    if ver.ok:
        payload["pg_version"] = ver.stdout.strip()
    n_mod = sum(1 for p in payload["params"]
                if p["source"] not in ("default", None))
    n_pend = sum(1 for p in payload["params"] if p["pending_restart"])
    _check("pg-settings", "ok",
           f"{len(payload['params'])} parameters ({n_mod} non-default"
           + (f", {n_pend} pending restart" if n_pend else "") + ")")

    print(f"{PARAMS_MARKER} {json.dumps(payload)}", flush=True)
    return 0
