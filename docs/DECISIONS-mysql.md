# MySQL support — decision log

One-line rationales for the non-obvious calls. The governing principle:
**one code path, one vocabulary, engine differences quarantined in
`pgbench_harness/engines.py`** — nothing downstream of the spec branches on
engine except through that module.

* **`target.engine` on the spec and the saved target, defaulting to
  `postgres`**: every existing spec, target row, job and run dir stays valid
  with zero migration work beyond one additive column (migration 10).
* **Password via `MYSQL_PWD`, never argv**: libmysqlclient reads `MYSQL_PWD`
  whenever no explicit password option is supplied — the exact twin of
  libpq's `PGPASSWORD` contract the harness was built on. sysbench's mysql
  driver leaves the password option unset unless given, so the env fallback
  applies there too. argv stays loggable for both engines, and the
  results-dir leak gate applies unchanged (proven by the MySQL worker e2e).
* **`sslmode` keeps PostgreSQL spelling in specs/targets for both engines**:
  one spec schema, one UI select, one stored shape; the engines module maps
  it (`require→REQUIRED`, `verify-ca→VERIFY_CA`, `verify-full→
  VERIFY_IDENTITY`) at the client/driver boundary. For sysbench's coarser
  `--mysql-ssl`, anything but `disable` means `on`.
* **Statement timeout via `--init-command=SET SESSION max_execution_time`**:
  the mysql client has no statement-timeout flag; the session variable caps
  SELECTs (the read probe and every collector query), and the subprocess
  timeout stays the hard stop for everything else — same layering as
  PGOPTIONS + hard cap on the PG side.
* **Engine metrics are MAPPED, not added**: the sampler/collector emit the
  same keys PG does (blks_hit = buffer-pool requests − physical reads, so
  cache-hit % is the buffer-pool hit ratio; wal_bytes = redo written;
  dead_tup = InnoDB history list length — the purge-lag/bloat analogue).
  This keeps parsing, rollups, CSV, SSE, charts and reports engine-free.
  Metrics with no honest analogue (checkpoints, archiver, replica replay
  lag from the primary side) are left blank rather than faked.
* **Dataset conformance via information_schema scoped to `database()`**:
  MySQL has no search_path; the default schema IS where sysbench's
  unqualified names resolve, so that scope answers the same question
  to_regclass answers on PG.
* **`suite.pgbench` and `device_probe` refused for MySQL at validation**:
  pgbench is a PostgreSQL tool and the device probe mounts a PG cluster's
  PVC; failing fast with a hint beats a mid-suite tool error.
* **PgBouncer pooler probe records n/a on MySQL**: there is no safe,
  universal ProxySQL/router probe over a plain SQL connection; recording
  n/a is honest metadata, not a gap.
* **One fake client per engine** (`tests/fakebin/mysql` beside `psql`),
  sharing the `prepared` marker and state dir, so mixed-engine test
  environments exercise exactly the dispatch the production code does.
