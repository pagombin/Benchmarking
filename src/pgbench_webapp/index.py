"""Reconcile the filesystem ``results/`` tree (the source of truth) into the
SQLite ``runs`` index, so runs created by the CLI directly also appear in the UI.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Optional

import yaml

from pgbench_webapp import queries


def _peak_qps(run_dir: Path, mode: str) -> Optional[float]:
    if mode == "soak":
        p = run_dir / "parsed" / "soak_summary.json"
        if p.exists():
            try:
                return float(json.loads(p.read_text())["baseline"]["tps"]) or None
            except (ValueError, KeyError, OSError):
                return None
        return None
    p = run_dir / "parsed" / "summary.json"
    if not p.exists():
        return None
    try:
        levels = json.loads(p.read_text())["levels"]
        vals = [l["qps_avg"] for l in levels if l.get("qps_avg") is not None]
        return max(vals) if vals else None
    except (ValueError, KeyError, OSError):
        return None


def _run_row(run_dir: Path) -> Optional[dict[str, Any]]:
    man = run_dir / "manifest.json"
    if not man.exists():
        return None
    try:
        m = json.loads(man.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    run_meta: dict[str, Any] = {}
    spec_path = run_dir / "spec.yaml"
    if spec_path.exists():
        try:
            run_meta = (yaml.safe_load(spec_path.read_text(encoding="utf-8")) or {}).get("run", {})
        except yaml.YAMLError:
            run_meta = {}
    mode = m.get("mode", "sweep")
    workload = ""
    try:
        workload = (yaml.safe_load((run_dir / "spec.yaml").read_text())).get("workload", {}).get("type", "")
    except (OSError, yaml.YAMLError, AttributeError):
        pass
    return {
        "run_id": m.get("run_id") or run_dir.name,
        "label": m.get("label", ""), "edition": m.get("edition", ""),
        "tshirt_size": m.get("tshirt_size", ""), "mode": mode,
        "workload_type": workload, "status": m.get("status", ""),
        "tags": ",".join(run_meta.get("tags", []) or []),
        "ticket": run_meta.get("ticket", ""), "owner": run_meta.get("owner", ""),
        "environment": run_meta.get("environment", ""),
        "peak_qps": _peak_qps(run_dir, mode),
        "created_utc": m.get("created_utc", ""), "finished_utc": m.get("finished_utc", ""),
        "source": "fs",
    }


def reconcile(conn: sqlite3.Connection, results_dir: Path) -> int:
    """Upsert every run directory under *results_dir* into the index."""
    if not results_dir.exists():
        return 0
    n = 0
    for d in sorted(results_dir.iterdir()):
        if not d.is_dir():
            continue
        row = _run_row(d)
        if row is not None:
            queries.upsert_run(conn, row)
            n += 1
    return n
