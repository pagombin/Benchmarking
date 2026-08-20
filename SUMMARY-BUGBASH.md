# Bug bash + enterprise UI hardening — summary

Branch: `feature/bugbash-enterprise-ui` (on top of the continuous-mode work).
Full ledger: [docs/BUGBASH.md](docs/BUGBASH.md) · decisions:
[docs/DECISIONS-bugbash.md](docs/DECISIONS-bugbash.md).

End state: **pytest green** (full suite incl. the new journey test),
**`mypy src/pgbench_harness` = 0 errors**, clean SPA production build
committed to `src/pgbench_webapp/static/spa/`.

## Bug counts

| Severity | Count | IDs |
|----------|------:|-----|
| P0 | 0 | — |
| P1 | 4 | B-001, B-002, B-014, B-026 |
| P2 | 10 | B-003…006, B-008, B-011…013, B-016, B-018 |
| P3 | 5 | B-007, B-009, B-010, B-021 (documented), B-027 |
| Verified-sound (no bug, behavior now proven by test) | 8 | B-015, B-017, B-019, B-020, B-022…025 |

Every fix has a regression test except the three pure-UI items (B-008/9/10),
which are build-verified per the brief (no new frontend test framework).

## Top 5 fixes by impact

1. **B-001 — stop racing the relaunch loop could resurrect a stopped
   workload.** The requeue is now a conditional UPDATE keyed on
   `desired_state='running' AND state != 'canceling'`, and a claimed
   continuous job re-checks intent before exec. Explicit stop now wins every
   interleaving; proven by two race tests plus the journey test.
2. **B-014 — any prober exit closed open outages ("job stopped"),** so a
   transient prober restart falsified outage durations and MTTR — the two
   numbers this whole feature exists to report. Open rows now close only
   when the job is genuinely inactive; otherwise the next tracker re-adopts
   them.
3. **B-016 — no worker singleton.** Two workers on one data dir would
   double-ingest, double-probe, and fight over reconcile. An exclusive
   flock on `data_dir/worker.lock` makes the second worker exit loudly —
   and the same lock powers the new `/api/worker/status` probe and the
   header "worker down" chip, so a dead worker is visible instead of a
   silently growing queue.
4. **B-026 — every continuous chart rendered blank** (axes and grid, no
   series, no outage shading). uPlot defers its first paint to an animation
   frame; ContChart's mount effects called `redraw()` synchronously in the
   construction commit, corrupting the x-scale before that first paint.
   Found by driving the built SPA against a seeded console in headless
   Chromium and probing canvas pixels; fixed with a mount-tick guard.
5. **B-005 — deploy.sh's fallback systemd units had lost the entire
   hardening block** (UMask/Protect*/Restrict*…) relative to
   `packaging/systemd/*`. Re-synced directive-for-directive, with a test
   that extracts the heredocs and diffs them against the packaged units so
   drift is now structurally impossible. (Honorable mention: B-012 — the 43
   pre-existing mypy errors, two hiding latent runtime bugs; all fixed.)

## UI: before → after

| Area | Before | After |
|------|--------|-------|
| Navigation | One flat link row; Continuous/Alerts/Outages unreachable except by URL | Grouped sidebar IA: Workloads / Fleet / Observability / Administration, RBAC-filtered (hide, don't disable) |
| Platform health | Nothing — a dead worker just meant jobs sat "queued" | Header chip polling `/api/worker/status`: "worker ok · N active" / "worker down" / "console degraded" |
| Alerts | Per-job tab only | Global console: window/severity/type/open filters, frequency summary (count + open + MTTR per type), delivery status column, CSV export |
| Outages | Per-job tab only | Global console: per-target availability table (interval-union uptime, longest outage, planned/unplanned/load split), filterable ledger, CSV export |
| Continuous detail | Charts + tables | + "Export report" (self-contained HTML snapshot of the KPI band + outage ledger for the selected window) and a raw-CSV download; URL-persisted windows already shipped in Part 7 |
| Continuous fleet board | Health dot, spark, uptime | + last-alert chip per row |
| Wrong-page traps | Continuous runs opened the sweep cockpit (B-008); pasted `continuous:` specs confused New run (B-009); Tasks filter hid new kinds (B-010) | RunDetail redirects to the continuous view; New run refuses with a pointer to the Continuous page; Tasks kinds derive from the data |
| Page identity | Every tab said the same thing | Per-route `document.title` on all main pages; footer shows `version · git SHA` |
| Settings | Raw thresholds only | Labeled threshold form + "Restore defaults" (client-side, applies on Save) |
| Time handling | Mostly UTC, some raw ISO strings | Decided once: UTC-first everywhere (fmtWhen/relAge), raw ISO only in tooltips, "times are UTC" in the header |

## Deferred (deliberately)

* **Audit-table retention** (B-021): append-only by design; auto-pruning an
  audit trail is an owner policy call. Export + bounded reads exist.
* **CSV convenience file growth** for continuous runs: the parsed
  `cont_timeseries.csv` grows unbounded on disk (the DB side is pruned).
  Documented in DECISIONS-continuous; segment logs are the source of truth.
* **Toast system / modal confirms**: kept `window.confirm` + inline banners
  (consistent with the existing console); a dialog framework is
  redesign-scale against the brief's scope cap.
* **Frontend unit tests**: per the brief, no new test framework; UI
  correctness rides on tsc, the production build, and API route tests.
* **Guided first-run wizard**: the Continuous create form got the
  saved-target-only flow with inline explanations; a multi-step wizard was
  cut from the bottom of Part 2 under the priority rule.

## Top 3 remaining risks

1. **Client-side clock trust in the fleet math.** The Outages console's
   uptime table uses the browser's clock for "now" when clipping open
   outages; a badly skewed client shows slightly wrong percentages (the
   backend summary endpoint remains authoritative and is what the per-job
   KPIs use).
2. **SQLite under sustained multi-workload load.** The busy paths are
   tested (B-018/019/020) and WAL+busy_timeout are set, but 16 continuous
   workloads × 1s samples on one droplet approaches the design's comfort
   zone; the collector/ingest cadences may need tuning on small droplets.
3. **Alert-rule coverage is threshold-based only.** Error-rate, latency,
   TPS-drop, no-data, disk — all static thresholds against rollups. Nothing
   detects slow degradation inside the thresholds (e.g. p99 creeping 10%/day);
   that class of regression still needs a human reading the 30d charts.
