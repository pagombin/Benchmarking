"""Management commands: `python -m pgbench_webapp.admin create-admin --user X`.

Reads the password from PGBENCH_ADMIN_PASSWORD or an interactive hidden prompt,
hashes it (bcrypt), and upserts an admin user. Never echoes or stores plaintext.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys

from pgbench_webapp import queries
from pgbench_webapp.config import ensure_dirs, load_config
from pgbench_webapp.db import connect, migrate
from pgbench_webapp.security import hash_password


def create_admin(username: str, password: str) -> None:
    cfg = load_config()
    ensure_dirs(cfg)
    migrate(cfg.db_path)
    conn = connect(cfg.db_path)
    try:
        queries.upsert_admin(conn, username, hash_password(password))
        queries.audit(conn, username, "admin_upsert", target=username, detail="create-admin")
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="pgbench_webapp.admin")
    sub = p.add_subparsers(dest="cmd", required=True)
    ca = sub.add_parser("create-admin", help="create or update the admin user")
    ca.add_argument("--user", default="admin")
    args = p.parse_args(argv)
    if args.cmd == "create-admin":
        password = os.environ.get("PGBENCH_ADMIN_PASSWORD") or getpass.getpass("Admin password: ")
        if not password:
            print("error: empty password", file=sys.stderr)
            return 2
        create_admin(args.user, password)
        print(f"admin user '{args.user}' created/updated.")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
