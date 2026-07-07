"""Worker-side plumbing for Cluster Ops jobs.

Keeps ``worker.py`` small: this module knows how to build the ``ops`` argv,
inject KUBECONFIG into the child environment (path-reference or decrypted
copy — contents registered with the redactor either way, so they can never
reach the job log), and index finished op runs into SQLite from the
``results/ops/`` filesystem (which stays the source of truth).
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Optional

from pgbench_harness.ops.oprun import TERMINAL as OPS_TERMINAL
from pgbench_harness.ops.oprun import read_meta
from pgbench_harness.util import atomic_write_json, get_redactor
from pgbench_webapp import queries
from pgbench_webapp.config import Config
from pgbench_webapp.secrets_store import SecretStore
from pgbench_webapp.util import utc_now_iso

# job kind -> ops CLI subcommand. Kinds with a run dir get --results-dir.
OPS_KINDS: dict[str, str] = {
    "ops_validate": "validate",
    "ops_discover": "discover",
    "ops_cr_apply": "cr-apply",
    "ops_backup": "backup",
    "ops_scenario": "scenario",
    "ops_monitor": "monitor",
}
RUN_DIR_KINDS = ("ops_cr_apply", "ops_backup", "ops_scenario", "ops_monitor")

SUMMARY_MARKER = "OPS_SUMMARY_JSON"
TOPOLOGY_MARKER = "OPS_TOPOLOGY_JSON"
SCHEDULES_MARKER = "OPS_SCHEDULES_JSON"


def is_ops_kind(kind: str) -> bool:
    return kind in OPS_KINDS


def build_argv(cfg: Config, kind: str, spec_file: Path) -> list[str]:
    argv = [cfg.harness_bin, "ops", OPS_KINDS[kind], "--ops-spec", str(spec_file)]
    if kind in RUN_DIR_KINDS:
        argv += ["--results-dir", str(cfg.results_dir)]
    return argv


def kubeconfigs_dir(cfg: Config) -> Path:
    d = cfg.data_dir / "kubeconfigs"
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    return d


def kubeconfig_ref(name: str) -> str:
    return f"kube:{name}:kubeconfig"


_KUBECONFIG_SECRET_KEYS = re.compile(
    r"^\s*(certificate-authority-data|client-certificate-data|client-key-data|"
    r"token|password|refresh-token|access-token|id-token|client-secret)\s*:\s*(.+)$")


def _register_kubeconfig_secrets(content: str) -> None:
    """Register the credential-bearing kubeconfig values with the redactor.

    A kubeconfig is multi-line, so registering the whole string would only
    catch verbatim full-document echoes; per-value registration catches the
    dangerous parts (certificate data, tokens, passwords) wherever they
    surface. Non-secret values (server URL, context names) stay readable —
    the validate op deliberately reports the API server URL to the UI.
    """
    red = get_redactor()
    for line in content.splitlines():
        m = _KUBECONFIG_SECRET_KEYS.match(line)
        if m:
            val = m.group(2).strip().strip("'\"")
            if len(val) >= 8:
                red.register(val)


def prepare_env(cfg: Config, store: SecretStore, env: dict[str, str],
                kube_target: sqlite3.Row, job_id: int) -> Optional[Path]:
    """Set KUBECONFIG in the child env. Returns a temp path to clean up (or None).

    Path mode: point at the operator-managed file (must live under the data
    dir when running under the shipped systemd sandbox). Ref mode: decrypt the
    imported copy to a 0600 file for the duration of the job.
    """
    tmp: Optional[Path] = None
    if kube_target["kubeconfig_ref"]:
        content = store.get(kube_target["kubeconfig_ref"])
        if content is None:
            raise RuntimeError(f"kube target '{kube_target['name']}': stored kubeconfig "
                               "missing from the secret store")
        _register_kubeconfig_secrets(content)
        tmp = kubeconfigs_dir(cfg) / f".job_{job_id}.kubeconfig"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        env["KUBECONFIG"] = str(tmp)
    else:
        path = kube_target["kubeconfig_path"]
        env["KUBECONFIG"] = path
        try:
            _register_kubeconfig_secrets(Path(path).read_text(encoding="utf-8"))
        except OSError:
            pass    # unreadable path — the validate op reports this properly
    return tmp


def parse_op_run_id(output: str, results_dir: Path) -> Optional[str]:
    """Extract the op run id from the runner's 'ops run -> <dir>' line."""
    m = re.search(re.escape(str(results_dir)) + r"/ops/([A-Za-z0-9][A-Za-z0-9._-]*)", output)
    return m.group(1) if m else None


def _ops_run_row(cfg: Config, op_run_id: str, job: sqlite3.Row) -> Optional[dict[str, Any]]:
    meta = read_meta(cfg.results_dir / "ops" / op_run_id)
    if meta is None:
        return None
    return {
        "op_run_id": op_run_id,
        "kind": meta.get("op", ""),
        "kube_target_id": job["kube_target_id"],
        "kube_target_name": (meta.get("target") or {}).get("name", ""),
        "label": meta.get("label", ""),
        "params": json.dumps(meta.get("params") or {}),
        "status": meta.get("status", ""),
        "linked_run_id": (meta.get("params") or {}).get("linked_run_id", "") or
                         (meta.get("headline") or {}).get("linked_run_id", ""),
        "headline": json.dumps(meta.get("headline") or {}),
        "created_utc": meta.get("created_utc", ""),
        "finished_utc": meta.get("finished_utc", ""),
    }


def index_ops_run(cfg: Config, conn: sqlite3.Connection, op_run_id: str,
                  job: sqlite3.Row) -> None:
    row = _ops_run_row(cfg, op_run_id, job)
    if row:
        queries.upsert_ops_run(conn, row)


def converge_ops_run(cfg: Config, op_run_id: str, job_state: str) -> None:
    """Drive a stuck non-terminal meta.json to terminal when its job has ended
    (mirror of index.converge_run_status for benchmark runs)."""
    if job_state not in ("done", "failed", "canceled"):
        return
    path = cfg.results_dir / "ops" / op_run_id / "meta.json"
    meta = read_meta(path.parent)
    if meta is None or meta.get("status") in OPS_TERMINAL:
        return
    meta["status"] = "canceled" if job_state == "canceled" else "failed"
    if not meta.get("finished_utc"):
        meta["finished_utc"] = utc_now_iso()
    atomic_write_json(path, meta)


def _marker_payload(log_path: Path, marker: str) -> Optional[dict[str, Any]]:
    """Last occurrence of ``MARKER {json}`` in the job output, parsed."""
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    payload: Optional[dict[str, Any]] = None
    for line in text.splitlines():
        if line.startswith(marker + " "):
            try:
                obj = json.loads(line[len(marker) + 1:])
                if isinstance(obj, dict):
                    payload = obj
            except ValueError:
                continue
    return payload


def postprocess(cfg: Config, conn: sqlite3.Connection, job: sqlite3.Row,
                state: str, op_run_id: Optional[str], log_path: Path) -> None:
    """After an ops job ends: cache validate/discover results onto the kube
    target, persist schedule-pause state, and index/converge the op run."""
    kt_id = job["kube_target_id"]
    kind = job["kind"]
    if kt_id and kind == "ops_validate":
        summary = _marker_payload(log_path, SUMMARY_MARKER)
        if summary:
            fields: dict[str, Any] = {"api_server": summary.get("api_server", ""),
                                      "last_validated_utc": utc_now_iso()}
            kt = queries.get_kube_target(conn, kt_id)
            # Pre-fill discovered names only where the operator left them blank.
            if kt is not None:
                if summary.get("cr_kind") and not kt["cr_name"]:
                    fields["cr_kind"] = summary["cr_kind"]
                    if len(summary.get("cr_names") or []) == 1:
                        fields["cr_name"] = summary["cr_names"][0]
                if summary.get("pguser_secret") and not kt["pguser_secret"]:
                    fields["pguser_secret"] = summary["pguser_secret"]
            queries.update_kube_target(conn, kt_id, **fields)
    if kt_id and kind == "ops_discover":
        topo = _marker_payload(log_path, TOPOLOGY_MARKER)
        if topo:
            fields = {"topology_json": json.dumps(topo), "topology_utc": utc_now_iso()}
            kt = queries.get_kube_target(conn, kt_id)
            if kt is not None and topo.get("cr_name") and not kt["cr_name"]:
                fields["cr_kind"] = topo.get("cr_kind") or kt["cr_kind"]
                fields["cr_name"] = topo["cr_name"]
            queries.update_kube_target(conn, kt_id, **fields)
    if kt_id:
        sched = _marker_payload(log_path, SCHEDULES_MARKER)
        if sched is not None:
            if sched.get("paused"):
                queries.update_kube_target(
                    conn, kt_id,
                    schedules_snapshot=json.dumps(sched.get("snapshot") or {}),
                    schedules_paused_utc=utc_now_iso())
            else:
                queries.update_kube_target(conn, kt_id, schedules_snapshot=None,
                                           schedules_paused_utc=None)
    if op_run_id:
        converge_ops_run(cfg, op_run_id, state)
        index_ops_run(cfg, conn, op_run_id, job)
