"""Kubeconfig / Kube Target validation — a live checklist, like preflight.

Emits one JSON object per line to stdout (``{"name", "status", "detail"}``),
which the webapp's job stream renders as a checklist, and finishes with a
single ``OPS_SUMMARY_JSON {...}`` line the worker parses to cache the API
server URL, context, namespaces, and discovered CR/secret names onto the
Kube Target row.
"""

from __future__ import annotations

import json
import os
import stat
from typing import Any

from pgbench_harness.ops.kube import Kube, KubeError
from pgbench_harness.ops.opspec import CR_KINDS, OpsSpec

SUMMARY_MARKER = "OPS_SUMMARY_JSON"

_SANDBOX_HINT = (
    "The service runs sandboxed (systemd ProtectHome=true / ProtectSystem=strict), "
    "which makes /root and /home unreadable even when running as root. Copy the "
    "kubeconfig under the data dir's kubeconfigs/ directory (e.g. "
    "/var/lib/pgbench-harness/kubeconfigs/<name>.yaml) and point the target there, "
    "or register it via the UI's 'paste contents' option (stored encrypted).")


def _check(name: str, status: str, detail: str = "") -> dict[str, Any]:
    obj = {"name": name, "status": status, "detail": detail}
    print(json.dumps(obj), flush=True)
    return obj


def _probe_kubeconfig(kc: str) -> tuple[bool, str]:
    """Return (ok, message) for the kubeconfig path WITHOUT ever raising.

    ``Path.is_file()`` re-raises PermissionError (EACCES) on Python 3.11+ rather
    than returning False, so a sandbox-hidden path (/root, /home under
    ProtectHome) would crash validate. Probe with os.stat + a real read and
    turn every failure into a clear, actionable check line.
    """
    try:
        st = os.stat(kc)
    except FileNotFoundError:
        # ProtectHome can present /root and /home as empty (files read as
        # not-found), so the sandbox hint applies here too.
        return False, f"kubeconfig not found at {kc}. " + _SANDBOX_HINT
    except PermissionError:
        return False, f"kubeconfig at {kc} is not accessible to the worker. " + _SANDBOX_HINT
    except OSError as exc:
        return False, f"kubeconfig at {kc} is not accessible: {exc}. " + _SANDBOX_HINT
    if not stat.S_ISREG(st.st_mode):
        return False, f"{kc} is not a regular file"
    try:
        with open(kc, "rb") as fh:
            fh.read(1)
    except OSError as exc:
        return False, f"kubeconfig at {kc} exists but the worker cannot read it: {exc}. " + _SANDBOX_HINT
    return True, "file readable"


def run_validate(spec: OpsSpec) -> int:
    """Validate the kubeconfig and target coordinates. Returns an exit code."""
    t = spec.target
    summary: dict[str, Any] = {"ok": False, "context": t.context, "api_server": "",
                               "namespaces": [], "cr_kind": "", "cr_names": [],
                               "pguser_secret": ""}
    failed = False

    # 1. KUBECONFIG present and the file visible to THIS process. Under the
    # shipped systemd hardening (ProtectHome=true, ProtectSystem=strict) a
    # kubeconfig outside /var/lib/pgbench-harness is invisible to the worker,
    # not merely unreadable — say so explicitly.
    kc = os.environ.get("KUBECONFIG", "")
    if not kc:
        _check("kubeconfig", "fail", "KUBECONFIG not set in the worker environment")
        failed = True
    else:
        kc_ok, kc_msg = _probe_kubeconfig(kc)
        _check("kubeconfig", "ok" if kc_ok else "fail", kc_msg)
        failed = failed or not kc_ok

    kube = Kube(context=t.context, namespace=t.namespace)

    # Steps 2-7 shell out to kubectl, which can raise KubeError (missing binary,
    # timeout on a hung API server). Guard the whole block so validate ALWAYS
    # emits its checklist + the OPS_SUMMARY_JSON line the worker parses — a
    # crash here would defeat validate's entire purpose (reporting exactly
    # these failure modes cleanly).
    try:
      # 2. kubectl client available.
      if not failed:
        res = kube.run(["version", "--client", "-o", "json"], namespaced=False)
        if res.ok:
            ver = ""
            try:
                ver = (json.loads(res.stdout).get("clientVersion") or {}).get("gitVersion", "")
            except (ValueError, AttributeError):
                pass
            _check("kubectl", "ok", ver or "client present")
        else:
            _check("kubectl", "fail", (res.stderr or res.stdout).strip()[:300])
            failed = True

      # 3. Context resolvable + API server URL (minified view exposes only the
      # server URL — never dump the full config, it contains credentials).
      if not failed:
          args = ["config", "view", "--minify",
                  "-o", "jsonpath={.clusters[0].cluster.server}"]
          res = kube.run(args, namespaced=False)
          if res.ok and res.stdout.strip():
              summary["api_server"] = res.stdout.strip()
              _check("context", "ok", f"API server {summary['api_server']}")
          else:
              _check("context", "fail",
                     f"context '{t.context or '(current)'}' not resolvable: "
                     f"{(res.stderr or res.stdout).strip()[:300]}")
              failed = True

      # 4. Cluster reachable + authorized: list namespaces (fall back to can-i).
      if not failed:
          res = kube.run(["get", "ns", "-o", "name"], namespaced=False, timeout_s=20)
          if res.ok:
              summary["namespaces"] = [ln.split("/", 1)[-1]
                                       for ln in res.stdout.splitlines() if ln.strip()]
              _check("cluster", "ok", f"{len(summary['namespaces'])} namespaces visible")
          else:
              can = kube.run(["auth", "can-i", "list", "pods"], timeout_s=20)
              if can.ok and can.stdout.strip().lower().startswith("yes"):
                  _check("cluster", "ok", "authorized for pods (namespace list denied)")
              else:
                  _check("cluster", "fail", (res.stderr or res.stdout).strip()[:300])
                  failed = True

      # 5. Target namespace exists (when we could list; otherwise probe it).
      if not failed:
          if summary["namespaces"] and t.namespace not in summary["namespaces"]:
              _check("namespace", "fail", f"namespace '{t.namespace}' not found")
              failed = True
          else:
              res = kube.run(["get", "pods", "-o", "name"], timeout_s=20)
              if res.ok:
                  _check("namespace", "ok",
                         f"'{t.namespace}': {len(res.stdout.splitlines())} pods visible")
              else:
                  _check("namespace", "fail", (res.stderr or res.stdout).strip()[:300])
                  failed = True

      # 6. Discover the CR (kind fallback order) and pre-fill names.
      if not failed:
          for kind in ([t.cr_kind] + [k for k in CR_KINDS if k != t.cr_kind]):
              try:
                  doc = kube.json(["get", kind])
                  names = [i.get("metadata", {}).get("name", "")
                           for i in doc.get("items", [])]
                  names = [n for n in names if n]
                  if names:
                      summary["cr_kind"], summary["cr_names"] = kind, names
                      break
              except KubeError:
                  continue
          if summary["cr_names"]:
              _check("cluster-cr", "ok",
                     f"{summary['cr_kind']}: {', '.join(summary['cr_names'])}")
          else:
              _check("cluster-cr", "warn",
                     f"no {' / '.join(CR_KINDS)} resources found in '{t.namespace}'")

      # 7. pguser secret present (name from the spec or derived from the CR).
      if not failed:
          cr = t.cr_name or (summary["cr_names"][0] if summary["cr_names"] else "")
          secret = t.pguser_secret or (f"{cr}-pguser-{t.db_user}" if cr else "")
          if secret:
              res = kube.run(["get", "secret", secret, "-o", "name"], timeout_s=20)
              if res.ok:
                  summary["pguser_secret"] = secret
                  _check("pguser-secret", "ok", secret)
              else:
                  _check("pguser-secret", "warn", f"secret '{secret}' not found — set it "
                         "explicitly on the Kube Target if it has a different name")
          else:
              _check("pguser-secret", "warn", "no CR name known yet — secret not checked")

    except KubeError as exc:
        _check("cluster", "fail", f"kubectl error: {str(exc)[:250]}")
        failed = True
    except Exception as exc:  # noqa: BLE001 — always reach the summary
        _check("validate", "fail", f"unexpected error: {str(exc)[:200]}")
        failed = True

    summary["ok"] = not failed
    print(f"{SUMMARY_MARKER} {json.dumps(summary)}", flush=True)
    return 0 if not failed else 3
