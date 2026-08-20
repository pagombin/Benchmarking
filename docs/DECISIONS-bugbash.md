# Bug bash + UI hardening — decision log

One-line rationales for every non-obvious call. Bug ids reference
docs/BUGBASH.md.

## Part 1 (bug bash)

* **Worker singleton = flock, exit on conflict (B-016)**: an flock on the
  data dir is free, releases on process death automatically (no stale-pid
  files), and cannot false-positive across reboots. The second worker EXITS
  with a clear message rather than waiting — a queued-up second worker would
  otherwise spring to life on the first worker's crash with no operator
  awareness.
* **Prober close-on-exit is conditional on the job being stopped (B-014)**:
  open outage rows are shared state re-adopted by the next tracker; closing
  them on ANY loop exit falsified durations. "Job stopped" is the only exit
  where nothing will ever re-adopt.
* **Delivery bookkeeping failure leaves the row pending (B-018)** rather
  than retrying record_delivery inline: a duplicate Slack message on the
  next tick is a better failure mode than delivery stalling behind one row,
  and with busy_timeout=30s the window is already tiny.
* **Session pruning happens on login (B-007)**, not in worker housekeeping:
  the web tier owns its table, logins are the natural (and rate-limited)
  hook, and it keeps the worker from touching auth state.
* **Audit table intentionally not auto-pruned (B-021)**: silently deleting
  an audit trail is a policy decision the owner should make; reads are
  bounded and CSV export exists.
* **mypy pass style (B-012)**: minimal, behavior-preserving narrowing
  (asserts on invariants the code already relied on, explicit None checks,
  annotations) — no refactors inside ops modules overnight. The two latent
  runtime bugs found (stitch None-bound comparison, crconfig loop-variable
  clobber) got real fixes, not casts.
* **`run --resume` restricted to sweeps (B-004)**: suites re-plan from their
  manifest on a fresh `suite` invocation and continuous resumes via
  `continuous --run-dir`; letting `run --resume` open either replayed the
  wrong plan.
* **CSV downloads stream from disk (B-011)** via FileResponse: correctness
  fix (memory) with no API change; the continuous series joins the map as
  `which=continuous`.
* **The H journey test drives the real worker/harness/fakebin stack** but
  calls ingest/probe/engine ticks synchronously instead of starting the
  background threads: deterministic ordering, zero sleep-based flakiness,
  and each assertion still exercises the production functions unmodified.

## Part 2 (UI hardening)

* **No new frontend test framework** (per the brief): UI changes are kept
  small and obvious; correctness rides on tsc + the production build + API
  route tests.
* **Confirm pattern**: `window.confirm` with precise consequence text
  everywhere (consistent with the existing console) rather than a new modal
  component — a dialog component would be a redesign-scale change for one
  night and the brief caps dependency/scope growth.
* **RBAC chrome convention: hide, don't disable** — matching the existing
  pages (Targets hides the editor for viewers); mixed conventions were the
  actual bug.
* **Timestamps: keep the console UTC-first** (fmtWhen + relAge everywhere,
  raw ISO only in title tooltips): the whole backend, the ledgers, and the
  runbooks speak UTC; converting to local time would make the UI disagree
  with every artifact. This IS the "decide once" from the brief.
* **Nav IA**: grouped as Workloads (Runs/Continuous/New run/Tasks) /
  Fleet (DB targets/Clusters/Ops runs) / Observability (Alerts/Outages/
  Compare/Environment) / Administration — Continuous elevated to a
  first-class page, Alerts/Outages become global consoles instead of
  per-job-only tabs.
* **Alerts/Outages consoles reuse the existing global APIs** with client-side
  frequency summaries ("db_unreachable: N in 30d, MTTR …") computed from the
  fetched window — no new backend endpoints needed beyond what shipped with
  continuous mode.
* **Report-window export = CSV bundle + a printable summary** built client-
  side from data already on the page (KPI band + outage table), rather than
  new server rendering machinery.
