"""Console entry points used by the systemd units / installer.

``pgbench-web``    -> serve the app over TLS (or run `migrate`).
``pgbench-worker`` -> run the job worker loop.
"""

from __future__ import annotations

import sys
from typing import Any

from pgbench_webapp.config import ensure_dirs, load_config
from pgbench_webapp.db import migrate


def web_main(argv: list[str] | None = None) -> int:
    """Run the uvicorn TLS server, or `pgbench-web migrate`."""
    args = argv if argv is not None else sys.argv[1:]
    cfg = load_config()
    ensure_dirs(cfg)
    if args and args[0] == "migrate":
        n = migrate(cfg.db_path)
        print(f"migrations: applied {n}")
        return 0
    import uvicorn
    ssl_kwargs: dict[str, Any] = {}
    if cfg.tls_cert.exists() and cfg.tls_key.exists():
        ssl_kwargs = {"ssl_certfile": str(cfg.tls_cert), "ssl_keyfile": str(cfg.tls_key)}
    else:
        print(f"WARNING: TLS cert/key not found ({cfg.tls_cert}); serving without TLS. "
              "Run the installer or deploy.sh --regen-certs.", file=sys.stderr)
    uvicorn.run("pgbench_webapp.app:create_app", factory=True,
                host=cfg.bind, port=cfg.port, **ssl_kwargs)
    return 0


def worker_main(argv: list[str] | None = None) -> int:
    """Run the queue worker loop (the `pgbench-worker` service)."""
    from pgbench_webapp.worker import worker_loop
    worker_loop()
    return 0


if __name__ == "__main__":
    raise SystemExit(web_main())
