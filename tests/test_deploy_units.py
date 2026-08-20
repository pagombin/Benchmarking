"""deploy.sh's built-in fallback systemd units must stay directive-identical
to the packaged units — they had drifted (the fallback silently lacked the
hardening block), so this comparison is now enforced programmatically."""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# deploy.sh constants the heredocs interpolate (the install contract).
SUBST = {
    "${APP_DIR}": "/opt/pgbench-harness",
    "${DATA_DIR}": "/var/lib/pgbench-harness",
    "${LOG_DIR}": "/var/log/pgbench-harness",
    "${ENV_FILE}": "/etc/pgbench-harness.env",
    "${SECRETS_ENV_FILE}": "/etc/pgbench-harness.secrets.env",
    "${SVC_USER}": "pgbench",
    "${SVC_GROUP}": "pgbench",
    "${BIN_WEB}": "/opt/pgbench-harness/venv/bin/pgbench-web",
    "${BIN_WORKER}": "/opt/pgbench-harness/venv/bin/pgbench-worker",
}


def _directives(text: str) -> set[str]:
    """Key=value directive lines, comments stripped."""
    out = set()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "[", ";")):
            continue
        if "=" in line:
            out.add(line)
    return out


def _builtin_unit(unit_name: str) -> str:
    """Extract one heredoc from deploy.sh's write_builtin_unit and substitute
    the install-contract constants."""
    body = (REPO / "deploy.sh").read_text(encoding="utf-8")
    fn = body[body.index("write_builtin_unit() {"):]
    marker = f'"${{{ "WEB_UNIT" if "web" in unit_name else "WORKER_UNIT" }}}")'
    at = fn.index(marker)
    start = fn.index("<<EOF", at) + len("<<EOF\n")
    end = fn.index("\nEOF", start)
    text = fn[start:end]
    for var, val in SUBST.items():
        text = text.replace(var, val)
    assert "${" not in text, f"unsubstituted variable in builtin {unit_name}: " \
        f"{re.findall(r'[$]{[A-Z_]+}', text)}"
    return text


import pytest  # noqa: E402


@pytest.mark.parametrize("unit", ["pgbench-web.service", "pgbench-worker.service"])
def test_builtin_fallback_matches_packaged_unit(unit: str) -> None:
    packaged = _directives(
        (REPO / "packaging" / "systemd" / unit).read_text(encoding="utf-8"))
    builtin = _directives(_builtin_unit(unit))
    missing = packaged - builtin
    extra = builtin - packaged
    assert not missing, f"{unit}: builtin fallback lacks {sorted(missing)}"
    assert not extra, f"{unit}: builtin fallback adds {sorted(extra)}"


def test_worker_unit_keeps_the_reattach_contract() -> None:
    """KillMode=process is the load-bearing line: without it a worker restart
    kills the running benchmark instead of re-attaching to it."""
    for src in (
        (REPO / "packaging" / "systemd" / "pgbench-worker.service").read_text(),
        _builtin_unit("pgbench-worker.service"),
    ):
        assert "KillMode=process" in src
        assert "Restart=on-failure" in src
