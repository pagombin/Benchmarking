# Continuous Mode — decision log

Every judgment call made during the overnight build, with a one-line
rationale. Where the prompt pre-made a decision, it is followed and not
re-listed here.

## Process / environment

* **Branch**: developed on `claude/continuous-mode-benchmarking-cab4g2`
  (the session's designated branch, which the platform forbids deviating
  from) rather than the prompt's `feature/continuous-mode` — same history,
  different name.
* **mypy baseline**: `mypy src/pgbench_harness` reports **43 pre-existing
  errors** on the untouched main branch under every recent mypy
  (1.8/1.15/2.3, Python 3.11) — "must stay clean" did not hold at baseline
  in this environment. Contract applied: all NEW code type-checks clean and
  the error count never rises; the pre-existing errors are queued for the
  bug-bash static pass.

## Supervisor / harness

* **Segments are time-bounded (`segment_time_s`, default 6h)** purely for
  log rotation: an unbounded `--time=0` sysbench would grow one raw log file
  forever, which no retention pass could ever prune. A clean boundary exit
  relaunches immediately and is explicitly NOT a failure (no backoff, no
  relaunch counter, no event).
* **Planned-rotation detection** = exit 0, not timed out, and ≥ 90% of the
  expected interval lines — pure `rc == 0` would let an instantly-exiting
  well-behaved failure hot-loop, and a strict `== segment_time_s` count
  would misclassify a healthy segment that missed one report tick.
* **`report_interval_s` must be exactly 1** (same rule as soak): rollup
  `gap_s` and the gap-as-outage model assume a dense per-second timeline; a
  coarser interval would score healthy seconds as downtime.
* **Backoff = full jitter** (`uniform(0, base)`, floored at 0.1s) over the
  configured ladder — the AWS-recommended shape; auth errors jump straight
  to the ladder's max because retrying a bad password fast only hammers the
  target's auth path.
* **Ladder reset**: a segment that produced ≥ 30 samples resets the
  consecutive-failure count to 1 on its eventual crash — a long-healthy
  workload that hits a blip should restart fast, only churn climbs the
  ladder.
* **`max_consecutive_failures` reached ⇒ the supervisor stays alive** with
  the load stopped (state.json `load_stopped: true`) instead of exiting —
  the prompt requires probing/alerting to continue, and an exiting harness
  would be resurrected by the desired-state loop, defeating the give-up.
* **Resume (`--run-dir`) preflight is best-effort**: hard-failing preflight
  on a reboot relaunch would wedge exactly when the target is also still
  booting; the backoff ladder + prober own that outage. Fresh submissions
  still preflight hard (fail fast on misconfiguration).
* **No LivePgSampler for continuous runs**: it writes `pg_timeseries.csv`
  unboundedly for the process lifetime; the worker's DB-side collector
  (SQLite rows with retention) owns engine metrics instead.
* **Manifest statuses reuse the existing vocabulary**: `running`,
  `stopped` (new, added to `TERMINAL_RUN`), `failed` — the prompt's `error`
  status was folded into `failed` so every existing status consumer
  (cockpit SSE, converge logic, list pages) keeps working without a parallel
  code path.
* **Manifest keeps only the last 500 segment records** (plus a
  `segments_total` counter): the manifest is atomically rewritten per
  segment, so an unbounded list would make every rewrite slower forever;
  `raw/` and `events.jsonl` remain the full record.
* **CSV convenience view** (`parsed/cont_timeseries.csv`) is kept per the
  prompt but is NOT the ingest source and is not rotated by the harness;
  the raw segment logs are canonical (see ingest decision below). Documented
  in the runbook's disk-budget table.

## Worker / lifecycle

* **The ingester tails the RAW segment logs, not the CSV** (the prompt
  offered the choice): segments are append-only and pruned wholesale, so a
  per-(job, segment) byte cursor can never be silently invalidated by a
  rewrite — whereas any future CSV rotation would corrupt a byte offset into
  it. The samples PRIMARY KEY makes replays after cursor loss idempotent.
* **Reboot relaunch = re-queue the same job row** (state→queued) rather than
  spawning the harness from reconcile directly: the single-claimer worker
  loop then applies lane caps and normal bookkeeping to the relaunch, and
  the job id (which keys all metrics tables) stays stable.
* **Harness crashes mid-life are also relaunched** (housekeeping tick,
  30s holdoff) — the mission says "runs indefinitely until a user explicitly
  stops it"; only reboot-time reconcile would leave a crashed harness dead
  until the next reboot. The holdoff prevents a crash-looping binary from
  hot-looping the queue; the `harness_relaunch` info alert is rate-limited
  to one per 10 minutes per job so "never give up" cannot mint thousands of
  ledger rows a day.
* **Desired-state wins over out-of-band stops**: an operator `kill` of the
  harness or a bare `systemctl stop pgbench-worker` leaves
  `desired_state='running'` and the workload auto-resumes. Only the console/
  API stop (which persists `stopped` FIRST) is durable. Documented in the
  runbook as the feature working as designed.
* **One migration (9) for all Continuous tables** instead of one per phase:
  fewer migration rows, one atomic upgrade for populated databases, and the
  phases still landed as separate code commits.
* **Lane symmetry**: `running_count` now excludes `continuous` alongside
  `ops_monitor`, and `claim_next_job` restricts claimable kinds per-lane;
  the pre-existing behavior that a full bench lane also blocks monitor
  claims was left untouched (changing it was out of scope).
* **`stop_job_process` sets `desired_state='stopped'` for continuous jobs
  itself** (not only the API route), so every stop path — including the
  generic /api/jobs/{id}/stop and cancel — carries the durable intent.
* **`--prepare` only on the first launch** (no `run_id` yet): a reboot
  relaunch must go straight into the supervisor, not stall in a dataset
  check against a database that may be unreachable.
* **Continuous jobs are excluded from the run/soak email-notify path**: the
  alert engine owns their notifications; a stop would otherwise email
  "canceled" for a healthy deliberate action.

## Deploy / packaging

* **deploy.sh unchanged**: migrations already run on every update
  (`run_migrate`), the new code ships in the package, and the SPA bundle is
  committed under `static/spa/`. The worker keeps `Restart=on-failure`
  (matching the packaged unit and the built-in fallback writer): the worker
  loop never exits cleanly, so `always` would change nothing except masking
  a deliberate `systemctl stop`; reboot survival comes from
  `WantedBy=multi-user.target` + the desired-state reconcile, not from the
  restart policy.

## Metrics / ledger

* **Sample timestamps normalized to second precision** — the second is the
  natural key (one sysbench report per second); duplicate sub-second stamps
  at segment boundaries dedup via the PK.
* **An all-gap minute has NO rollup row**: absence is the gap signal; a
  synthesized row would have to be invented at read time anyway and would
  break the "recompute only minutes with new samples" invariant that
  protects old rollups from retention-pruned samples.
* **Reindex preserves rollup minutes older than the surviving raw
  coverage**: raw logs are pruned at the 72h horizon by design, so those
  older minutes are legitimately un-rebuildable — wiping them would destroy
  35 days of history to prove a 72h invariant.
* **Retention deletes are bounded per batch** (~5000 rows via a PK-ordered
  bound) so the housekeeping tick never holds a multi-second write lock
  against the 1 Hz ingester.
* **Clock-skew tolerance**: rollups are recomputed per touched minute from
  the samples table, so out-of-order/backdated samples (NTP step across a
  reboot) self-correct instead of corrupting aggregates.
* **Uptime counts UNPLANNED read/write outages only**; `load` outages are
  loadgen-side and reported separately — counting our own crashed load
  generator against the platform's availability would be dishonest.
  MTBF = window / incident count; MTTR = mean duration of in-window
  unplanned db outages (open outages clip to the window edge).
* **Latency percentiles over a window are percentiles OF the per-second
  p99** (p50/p95/p99 of `lat_p99`) — sysbench emits one percentile per
  interval, so this is the honest aggregation without raw event data.

## Prober / alerting

* **Canary table** `pgbench_harness_canary`, single row (`id=1` upserted):
  created if absent by the write probe itself; zero growth.
* **Outage `started_utc` is the FIRST failed probe** of the streak, not the
  down-transition — the outage began when the first request failed; M−1
  probes of "degraded" are detection latency, not health.
* **A restarted worker adopts open outage rows** instead of opening
  duplicates (tracker constructor scans for an open row) — a worker deploy
  during an outage must not split one incident into two.
* **Prober loops close their open outage rows when the job stops** ("job
  stopped" note) so a deliberate stop never leaves an eternal open outage
  inflating MTTR.
* **Alert delivery is decoupled from storage**: rows insert with
  `delivery IS NULL`; the engine delivers pending rows with 5× retry +
  jitter and records the outcome. With Slack unconfigured, rows are marked
  `none` (never eternally pending). A failed first push still sets
  `last_notified_utc` so standing crits retry on the renotify cadence.
* **Point alerts** (`harness_relaunch`, `db_recovered`, `load_resumed`) are
  stored pre-resolved: they are one-shot facts, and an unresolved info row
  would dedup-block the next occurrence forever.
* **Engine conditions honor maintenance windows at store time**
  (suppressed rows: kept in the ledger, marked `suppressed`, never sent);
  an outage that started OUTSIDE a window alerts normally even if a window
  is declared later — only its `planned` flag flips.
* **`loadgen_disk` uses 5-point hysteresis** (fires > 85%, resolves < 80%)
  so it cannot flap on the boundary.
* **`tps_drop` needs ≥ ~22h of minute rollups** (1320) before it arms —
  the prompt's "skip until 24h of history exists", with a small allowance
  for gap minutes.
* **Checkpoint-stats shape latch** copied from ops/monitor.py's field
  lesson: only latch onto pre-17 `pg_stat_bgwriter` when it actually
  returns data, keyed per target host.
* **heartbeat_url must be http(s)** — an admin-only setting, but a `file://`
  fetch via urllib would still be an SSRF-ish footgun.

## API / UI

* **`/api/runs` rejects continuous specs** with a pointer to
  `/api/continuous`: the generic path would enqueue them as kind `run` and
  bypass every lifecycle rule (saved target, desired_state, one-per-target).
* **No static HTML report for continuous runs** (the prompt's "simplest
  acceptable"): `report`/`generate_report` refuse with a clear message and
  the run-report route returns 400, not 500. The console view + CSV export
  is the reporting surface.
* **Fleet-list enrichment computes uptime/spark inline** (a few small
  queries per job): continuous jobs are capped at ~4–16, so N+1 queries are
  bounded and simpler than a bespoke aggregate query.
* **Custom range inputs are interpreted as UTC** (labelled in the UI) — the
  whole stack is UTC; a timezone-converting picker would be the only
  non-UTC surface and invite off-by-tz-offset bugs.
