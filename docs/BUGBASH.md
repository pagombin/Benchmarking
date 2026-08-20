# Bug bash ledger

Severity: **P0** data-loss/security/crash · **P1** wrong results / stuck
states · **P2** correctness edge / UX · **P3** polish. "Found" names the
methodology section (A lifecycle, B SQLite, C API, D CLI, E SSE, F deploy,
G frontend, H e2e) from the bug-bash brief.

| ID | Sev | Found | Where | Root cause | Fix | Regression test |
|----|-----|-------|-------|-----------|-----|-----------------|
| B-001 | P1 | A | worker/contworker | A stop racing the desired-state relaunch could resurrect an explicitly stopped workload: the requeue used a stale row read, and a claimed job never re-checked intent before exec | Requeue is a conditional UPDATE keyed on `desired_state='running' AND state != 'canceling'`; `run_job` re-checks desired_state for claimed continuous jobs before launching | `test_continuous_reconcile.py::test_requeue_loses_race_to_stop`, `::test_run_job_refuses_launch_when_desired_stopped` |
| B-002 | P1 | test infra | tests/fakebin/psql | New collector handlers matched substrings (`pg_stat_checkpointer`, `replay_lag`) that also appear in the LivePgSampler's consolidated row_to_json query — every sampler variant got hijacked and the sampler went blank | Handlers now match the exact SELECT prefix of their split query | `test_e2e.py::test_live_pg_sampler_writes_timeseries` (existing, was failing) |
| B-003 | P2 | D | compare.py | Two continuous runs slipped past the same-type check into the sweep renderer, which failed on a missing `summary.json` with an unrelated message | `compare_runs` refuses continuous runs up front with a pointer to the windowed console APIs | `test_bugbash.py::test_compare_refuses_continuous_cleanly` |
| B-004 | P2 | D | runner._init_run | `run --resume --run-dir <soak/continuous dir>` loaded the wrong-mode manifest and would replay a sweep plan against foreign artifacts | Resume validates `manifest.mode == "sweep"` with a hint per mode | `test_bugbash.py::test_run_resume_refuses_wrong_mode_dir` |
| B-005 | P2 | F | deploy.sh | The built-in fallback systemd units had drifted from `packaging/systemd/*` — the fallback lacked the entire hardening block (UMask, Protect*, Restrict*, KillSignal, TimeoutStopSec), so a droplet installed from a partial checkout silently ran weaker units | Fallback heredocs re-synced directive-for-directive; a test extracts them from deploy.sh and diffs against the packaged units so they can never drift again | `test_deploy_units.py` (3 tests) |
| B-006 | P2 | C | app.api_delete_run | Deleting a continuous run removed the job rows but left its samples/rollups/db metrics/cursors/outages/alerts — fleet-wide ledgers showed rows for jobs that no longer existed | `queries.purge_continuous_data` called for each deleted continuous job | `test_bugbash.py::test_delete_run_purges_continuous_tables` |
| B-007 | P3 | B | sessions table | Expired sessions were never deleted (only explicit logout removed rows) — unbounded growth | Opportunistic prune of expired rows on every successful login | `test_bugbash.py::test_expired_sessions_pruned_on_login` |
| B-008 | P2 | G | RunDetail/History | Continuous runs listed on the Runs page opened the sweep/soak cockpit, which has no continuous handling (empty charts, budget 0) | Part 2: RunDetail detects `mode: continuous` and routes to the Continuous view; History badges the mode | UI (build-verified) |
| B-009 | P3 | G | NewRun | `detectMode` didn't recognize a pasted `continuous:` spec (the server guard already 400s with a pointer) | Part 2: detect + steer to the Continuous page | UI (build-verified) |
| B-010 | P3 | G | Tasks | The kind filter was missing continuous/suite/probe/ops kinds | Part 2: filter list derived from the data | UI (build-verified) |
| B-011 | P2 | C | app.run_csv | No export for the continuous series, and every CSV download buffered the whole file in memory (a month-long series is hundreds of MB) | `which=continuous` added; endpoint now streams via FileResponse | `test_continuous_webapp.py` route coverage; manual size reasoning in DECISIONS |
| B-012 | P2 | static | pgbench_harness (17 files) | "mypy must stay clean" did not hold: 43 pre-existing errors, several hiding latent bugs — `crconfig` clobbered its warnings loop variable, `stitch._line_ts_ms` compared against a None window bound (latent TypeError), `operate` indexed repos with a possibly-None index, `pgbench_cmd` stuffed a float into an int field | All 43 fixed (Optional narrowing, annotations, loop-var rename, explicit None-bounds); `mypy src/pgbench_harness` = 0 errors on mypy 2.3.1 | mypy in CI usage; existing suites re-run green |
| B-013 | P2 | A | contworker no_data | The no_data crit could fire on the first engine tick after a worker boot: probe ticks are in-process, so a fresh worker looked "wedged" for one tick | Grace of one `no_data_s` after worker start before the rule arms | `test_alert_engine.py::test_no_data_condition` (extended) |
| B-014 | P1 | H | contprobe.probe_job_loop | ANY prober-loop exit closed open outage rows with "job stopped" — a transient prober restart (or bounded run) would falsely end a live outage, corrupting duration/MTTR | Close only when the job is genuinely no longer active; otherwise the next tracker re-adopts the open row | `test_bugbash.py::test_prober_exit_keeps_outage_open_unless_job_stopped`; journey step 7 |
| B-015 | ok | D | runner.cmd_mark | Verified: `mark` on a continuous run stamps at 'now' (usable annotation); `--at-s` refuses cleanly (soak-only anchor) | none needed | — |
| B-016 | P2 | A | worker_loop | Two worker processes against one data dir would double-probe, double-ingest, and race the reconcile (claims themselves were already IMMEDIATE-transaction safe) | Exclusive flock on `<data_dir>/worker.lock`; second worker exits with a clear message | `test_bugbash.py::test_worker_singleton_lock` |
| B-017 | ok | A | continuous supervisor | Verified: a stop during the relaunch backoff lands promptly (the sleep is interruptible) | none needed (behavior proven) | `test_bugbash.py::test_supervisor_stop_during_backoff_is_prompt` |
| B-018 | P2 | B | alerts.deliver_pending | One row's SQLITE_BUSY during delivery bookkeeping raised out of the loop, stalling every alert behind it until the next tick | Per-row guard; the row stays pending and retries | covered via `test_alert_engine.py` delivery tests |
| B-019 | ok | B | contmetrics.ingest_job | Verified: a locked DB mid-ingest loses nothing — the cursor only advances with the batch, and the PK dedups the retry | none needed (behavior proven) | `test_bugbash.py::test_ingest_locked_db_retries_without_loss` |
| B-020 | ok | B | contmetrics.prune | Verified: retention deletes run in bounded batches and still clear a large backlog completely | none needed (behavior proven) | `test_bugbash.py::test_batched_delete_clears_large_backlog` |
| B-021 | P3 | B | audit table | Grows forever by design (append-only record); export exists, API reads are bounded | Documented; retention policy left as an owner decision (an audit trail is deliberately not auto-pruned) | — |
| B-022 | ok | A | queries.claim_next_job | Verified: double-claim protection — the claim is a BEGIN IMMEDIATE transaction, plus B-016's process-level lock | documented | existing claim tests + `test_claim_lanes_are_independent` |
| B-023 | ok | E | app._sse / _job_sse | Verified: byte-offset incremental reads, bounded backfill (21600 rows), max_ticks ceilings, no DB connection pinned by run streams; non-UTF-8 handled with errors="replace" | none needed | existing SSE tests |
| B-024 | ok | C | ops_routes.ops_run_file | Verified: per-component `_safe_segment` makes traversal impossible on the artifact download path | none needed | `test_run_routes_reject_path_traversal` (existing) |
| B-025 | ok | H | whole stack | The full journey now exists as one test: config → target → prepare → continuous → samples/rollups → sysbench crash → reboot → relaunch+alert → probe outage → mocked Slack delivery record → stop → no resurrection → no secret in any artifact | — | `test_continuous_full_journey.py` |
| B-026 | P1 | G | ContChart.tsx | Every continuous chart rendered axes but **no series and no shading** — uPlot defers its first paint to a rAF, and the component's mount-time effects called `redraw()` synchronously in the same commit that constructed the plot, corrupting the x-scale before the first paint (axes ranges survive; series paths and x-splits come out empty, permanently) | A `fresh` ref set at construction makes the setData/redraw effects skip their mount-tick invocation; later (post-paint) updates behave as before | Found and verified against a live seeded console in headless Chromium (canvas pixel probe: 0 series-color pixels before, ~6k after); UI build-verified per the no-frontend-framework rule |

## Section walk summary

* **A (lifecycle)**: every job state (queued/running/canceling/done/failed/
  canceled × desired_state) and every prober/alert/outage transition walked;
  the two real holes found (B-001 race window, B-014 outage close) are fixed
  with tests. Reboot mid-transition is idempotent: requeue is conditional,
  ingest replays dedup on the PK, trackers re-adopt open outages.
* **B (SQLite)**: every long-lived thread owns its connection; no cross-
  thread sharing found. busy_timeout 30s + WAL; retention batched; WAL
  checkpointed hourly. B-018/019/020 prove the busy paths.
* **C (API)**: all list endpoints bounded (runs 500, jobs 500, audit 5000,
  alerts/outages 1000+offset, maintenance 500); traversal impossible on
  run_id/artifact paths (`_safe_segment` + resolved-parent check); user
  errors are 4xx (continuous report 400, range errors 400, unknown ids 404).
* **D (CLI)**: B-003/B-004 fixed; `mark`/`list`/`validate`/`report` verified
  tolerant of `mode: continuous`; redaction verified on the new supervisor/
  prober/collector paths (raw logs redacted at write; probe/psql stderr
  redacted; webhook scrubbed from test-endpoint errors).
* **E (SSE)**: verified (B-023).
* **F (deploy)**: B-005 fixed with a drift test; migrations re-run safe on
  populated DBs (existing test); every new setting reads through a default.
* **G (frontend)**: findings B-008/9/10 fixed in Part 2 (see
  SUMMARY-BUGBASH.md); api.ts already routes 401s to /login; formatters
  already guard NaN/null. Driving the built SPA against a seeded console in
  a real (headless) browser then surfaced B-026 — the continuous charts
  drew axes but never their series — which no amount of tsc/build checking
  could have caught.
* **H**: `test_continuous_full_journey.py` — the end-to-end drill in ~13s.
