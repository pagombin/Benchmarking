# Continuous Mode — overnight build summary

## What was built

**Continuous Mode**: an always-on benchmark workload (tpcc /
oltp_read_only / oltp_read_write / oltp_write_only) that runs against a
saved target **until a user explicitly stops it**, producing 30 days of
queryable metrics, a downtime/outage ledger, a stored alert history, and
Slack alerting. Eight phases, each committed separately with green
`pytest` (all 470+ tests) and a clean SPA build:

1. **Harness** (`pgbench_harness/continuous.py`, `spec.py`, `cli.py`):
   `continuous:` spec section; `pgbench-harness continuous` with
   `--run-dir` resume; a supervisor that relaunches sysbench forever with
   capped exponential backoff + full jitter, classifies auth vs network
   errors (auth → max backoff immediately), rotates raw segment logs every
   6h (planned rotation ≠ failure), heartbeats `state.json` every ~10s, and
   finalizes to `status: stopped` on SIGTERM.
2. **Reboot survival** (`worker.py`, `contworker.py`, migration 9):
   `jobs.desired_state` is the durable intent. Dead harness +
   desired-running ⇒ re-queue and relaunch into the SAME run directory
   (new segment) — at worker startup after a reboot, and from a
   housekeeping tick after a mid-life crash. Explicit stops persist
   `stopped` first, so nothing resurrects them. Continuous jobs get their
   own worker lane (`continuous_cap`) and never wedge the benchmark queue.
3. **Metrics pipeline** (`contmetrics.py`): worker-side ingester tails the
   raw segment logs (per-segment byte cursors, PK-idempotent replays) into
   `cont_samples` (1s, 72h) and `cont_rollup_1m` (with `gap_s`; 35d);
   batched retention; hourly WAL checkpoint;
   `pgbench-web reindex-continuous --job N` rebuilds from raw.
4. **Availability prober** (`contprobe.py`): read/write probes over psql
   (5s cadence, 3s timeouts), healthy→degraded→down at 3 failures, an
   outage ledger (read/write/load kinds) with durations and error classes,
   maintenance windows (planned outages, suppressed alerts), and open-row
   adoption across worker restarts.
5. **Alert engine + Slack** (`alerts.py`, `contworker.py`,
   `contcollect.py`): store-first history, dedup while unresolved, crit
   re-notify cadence, rules (db_unreachable, db_recovered, error_rate,
   latency, tps_drop vs 24h median, auth_failure, harness_relaunch,
   load_gap, loadgen_disk, no_data), Slack delivery with 5× retry/backoff
   recorded per row, per-channel `POST /api/notify/test`, dead-man
   heartbeat, and a 15s DB-side collector (split queries, raw counters).
6. **APIs** (`continuous_routes.py`): lifecycle (create/stop/resume with
   the saved-target rule and one-per-target), windowed
   timeseries/summary/dbmetrics (resolution selection, min-preserving
   decimation, 31-day cap), per-job and fleet-wide alert/outage ledgers,
   maintenance CRUD, extended admin settings — all RBAC'd and covered by
   route tests.
7. **Console** (`Continuous.tsx`, `ContinuousView.tsx`, `ContChart.tsx`,
   `Settings.tsx`): fleet board (health dot, uptime 24h, sparkline,
   open-outage badge, stop/resume), detail view (window pills + shareable
   custom UTC ranges, KPI band, charts with outage/maintenance shading and
   event markers, DB panels, ledgers, maintenance CRUD), settings
   (thresholds form, heartbeat, lane cap, Slack test with per-channel
   results). SPA rebuilt into `static/spa/`.
8. **Docs**: README Continuous section + spec reference, OPERATIONS.md
   §15 runbook (reboot behavior, disk budget, Slack/heartbeat pairing,
   threshold tuning, ledger reading, reindex, durability drill),
   CHANGELOG entry, `docs/DECISIONS-continuous.md` (every judgment call).

## Try it in 10 minutes on a droplet

```bash
# 0. install/update as usual (runs migrations, restarts services)
sudo ./deploy.sh --update

# 1. console → DB targets → save your Advanced cluster WITH its password
#    (continuous requires a saved target: the credential must survive reboots)

# 2. console → Continuous → Start a continuous workload
#    target = the saved cluster, workload = oltp_read_write, threads = 16,
#    leave "load the dataset first" checked → Start.
#    Within ~a minute the row shows running, a recent last-sample, and the
#    detail view starts filling its 10m window.

# 3. simulate a sysbench crash (supervisor relaunch):
sudo pkill -f 'sysbench.*report-interval'
#    → state.json relaunches++, a loadgen_restart event, charts continue.

# 4. simulate a deploy (KillMode=process re-attach):
sudo systemctl restart pgbench-worker      # the load never stops

# 5. simulate a reboot (or actually: sudo systemctl reboot):
sudo systemctl stop pgbench-worker && sudo pkill -f 'pgbench-harness continuous'
sudo systemctl start pgbench-worker
#    → the job re-queues, an info harness_relaunch alert lands (Slack if
#      configured), and a NEW cont_seg appears in the SAME results/<run_id>/raw/.

# 6. probes + alerting: Settings → Slack (enable + webhook) → "Send test
#    message" shows the per-channel result. Then break the DB password or
#    firewall briefly: after 3 failed probes an outage opens in the ledger
#    and a crit db_unreachable is stored + delivered; recovery closes it
#    with its duration.

# 7. stop it deliberately: Continuous → Stop (metrics/ledger retained;
#    Resume continues the same timeline).
```

## Deliberately deferred

* **Frontend tests** — no frontend test harness exists in the repo; UI
  correctness rides on `tsc`/build plus the API route tests (the bug-bash
  pass is the designated place to harden the UI).
* **A static "report window" export** for continuous runs — the console
  view + CSV endpoints are the reporting surface (per the prompt's
  "simplest acceptable"); a self-contained HTML export is queued as a
  bug-bash/UI-phase item.
* **`parsed/cont_timeseries.csv` rotation** — the convenience CSV grows
  ~9 MB/day for the run's lifetime (documented in the disk budget). The
  operative stores (raw segments, SQLite) are bounded; rotating the CSV
  under a live writer safely needs a small supervisor change I chose not to
  rush overnight.
* **Pre-existing mypy debt** — 43 errors exist on main under current mypy
  (documented in DECISIONS); new code is clean, and fixing the backlog is
  a bug-bash static-pass item.

## Risks to review first

1. **Desired-state auto-resume semantics**: killing the harness by hand (or
   stopping just the worker) leaves `desired_state='running'` and the
   workload WILL come back. This is the documented contract, but confirm
   you want out-of-band kills to be non-durable.
2. **Retention deletes vs a hot ingester** share one SQLite database; the
   deletes are batched and WAL-checkpointed hourly, and busy_timeout is
   30s, but a real droplet under heavy alert/outage churn is worth one
   morning's `journalctl -u pgbench-worker` review.
3. **The one-per-target rule keys on saved-target id**, not host string —
   two saved targets pointing at the same host can each run a workload.
   Deliberate (targets are the identity), but worth knowing.
4. **Probe load on the target**: 2 psql connections every 5s per workload
   (plus the 15s collector). Negligible for Advanced clusters, but visible
   in pg_stat_activity/connection graphs.
