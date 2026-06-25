"""Runtime configuration, read from environment with sane defaults.

The data directory is the single mutable root: SQLite index/queue, the secret
key, TLS certs, and the harness ``results/`` tree all live under it, so backup
is one directory and updates never touch it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    data_dir: Path
    db_path: Path
    results_dir: Path
    secret_key_path: Path
    tls_cert: Path
    tls_key: Path
    bind: str
    port: int
    harness_bin: str       # path to the pgbench-harness CLI the worker shells out to

    @property
    def certs_dir(self) -> Path:
        return self.tls_cert.parent


def load_config() -> Config:
    """Build the Config from PGBENCH_* env vars (used by web + worker + CLI)."""
    data_dir = Path(os.environ.get("PGBENCH_DATA_DIR", "/var/lib/pgbench-harness"))
    certs = data_dir / "certs"
    return Config(
        data_dir=data_dir,
        db_path=Path(os.environ.get("PGBENCH_DB", str(data_dir / "pgbench.db"))),
        results_dir=Path(os.environ.get("PGBENCH_RESULTS_DIR", str(data_dir / "results"))),
        secret_key_path=Path(os.environ.get("PGBENCH_SECRET_KEY", str(data_dir / "secret.key"))),
        tls_cert=Path(os.environ.get("PGBENCH_TLS_CERT", str(certs / "cert.pem"))),
        tls_key=Path(os.environ.get("PGBENCH_TLS_KEY", str(certs / "key.pem"))),
        bind=os.environ.get("PGBENCH_BIND", "0.0.0.0"),
        port=int(os.environ.get("PGBENCH_PORT", "8443")),
        harness_bin=os.environ.get("PGBENCH_HARNESS_BIN", "pgbench-harness"),
    )


def ensure_dirs(cfg: Config) -> None:
    """Create the data/results/certs directories if missing (idempotent)."""
    for d in (cfg.data_dir, cfg.results_dir, cfg.certs_dir):
        d.mkdir(parents=True, exist_ok=True)
