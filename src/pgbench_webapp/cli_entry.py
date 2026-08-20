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
    """Run the uvicorn TLS server, or a maintenance subcommand:

    ``pgbench-web migrate``                    apply DB migrations.
    ``pgbench-web reindex-continuous --job N`` wipe and rebuild one continuous
        job's SQLite series from its run directory's raw segment logs (the
        filesystem is the source of truth; SQLite is a rebuildable index).
    """
    args = argv if argv is not None else sys.argv[1:]
    cfg = load_config()
    ensure_dirs(cfg)
    if args and args[0] == "migrate":
        n = migrate(cfg.db_path)
        print(f"migrations: applied {n}")
        return 0
    if args and args[0] == "reindex-continuous":
        if len(args) < 3 or args[1] != "--job" or not args[2].isdigit():
            print("usage: pgbench-web reindex-continuous --job <job_id>",
                  file=sys.stderr)
            return 2
        migrate(cfg.db_path)
        from pgbench_webapp.contmetrics import reindex_continuous
        from pgbench_webapp.db import connect
        conn = connect(cfg.db_path)
        try:
            out = reindex_continuous(cfg, conn, int(args[2]))
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        finally:
            conn.close()
        print(f"reindexed job {args[2]}: {out['samples']} samples across "
              f"{out['minutes']} minute rollups")
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
