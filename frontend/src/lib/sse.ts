// Live cockpit transport. Wraps EventSource over the existing /runs/:id/stream.
// Each (re)connection begins with a `hello` event — the client treats that as a
// full reset and catches up from offset 0, so auto-reconnect never duplicates.

export interface Hello {
  run_id: string;
  mode: string;
  status: string;
  budget_s: number;
}

export interface Progress {
  status: string;
  elapsed_s: number;
  budget_s: number;
  levels_total: number;
  levels_done: number;
  current: string;
}

export interface SampleBatch {
  file: string;
  header: string;
  offset: number;
  rows: string[];
}

export interface StreamHandlers {
  onHello?: (h: Hello) => void;
  onLog?: (chunk: string) => void;
  onSamples?: (b: SampleBatch) => void;
  onPg?: (b: SampleBatch) => void;
  onProgress?: (p: Progress) => void;
  onDone?: (d: { status: string }) => void;
  onError?: () => void;
}

export function openStream(runId: string, h: StreamHandlers): EventSource {
  const es = new EventSource(`/runs/${encodeURIComponent(runId)}/stream`);
  es.addEventListener("hello", (e) => h.onHello?.(JSON.parse((e as MessageEvent).data)));
  es.addEventListener("log", (e) => h.onLog?.(JSON.parse((e as MessageEvent).data)));
  es.addEventListener("samples", (e) => h.onSamples?.(JSON.parse((e as MessageEvent).data)));
  es.addEventListener("pg", (e) => h.onPg?.(JSON.parse((e as MessageEvent).data)));
  es.addEventListener("progress", (e) => h.onProgress?.(JSON.parse((e as MessageEvent).data)));
  es.addEventListener("done", (e) => {
    h.onDone?.(JSON.parse((e as MessageEvent).data));
    es.close();
  });
  es.onerror = () => h.onError?.();
  return es;
}

// ── task jobs (preflight / prepare / doctor) ────────────────────────────

export interface CheckEvent {
  name: string;
  status: string; // ok | warn | fail | info
  detail: string;
}

export interface JobHandlers {
  onCheck?: (c: CheckEvent) => void;
  onLog?: (line: string) => void;
  onDone?: (d: { status: string }) => void;
  onError?: () => void;
}

export function openJobStream(jobId: number, h: JobHandlers): EventSource {
  const es = new EventSource(`/api/jobs/${jobId}/stream`);
  es.addEventListener("check", (e) => h.onCheck?.(JSON.parse((e as MessageEvent).data)));
  es.addEventListener("log", (e) => h.onLog?.(JSON.parse((e as MessageEvent).data)));
  es.addEventListener("done", (e) => {
    h.onDone?.(JSON.parse((e as MessageEvent).data));
    es.close();
  });
  es.onerror = () => h.onError?.();
  return es;
}

// ── series model ──────────────────────────────────────────────────────
// Columnar buffers for uPlot: t (elapsed seconds) + the five harness metrics.
// Sweep samples.csv uses `t_offset`; soak_timeseries.csv uses `t`.

export interface Series {
  t: number[];
  tps: number[];
  qps: number[];
  p99: number[];
  err: number[];
  reconn: number[];
}

export function emptySeries(): Series {
  return { t: [], tps: [], qps: [], p99: [], err: [], reconn: [] };
}

export function appendBatch(s: Series, batch: SampleBatch): void {
  const cols = batch.header.split(",");
  const ix = (name: string) => cols.indexOf(name);
  const it = ix("t_offset") >= 0 ? ix("t_offset") : ix("t");
  const itps = ix("tps");
  const iqps = ix("qps");
  const ip99 = ix("lat_p99");
  const ierr = ix("err_s");
  const irec = ix("reconn_s");
  for (const line of batch.rows) {
    const f = line.split(",");
    const t = parseFloat(f[it]);
    if (Number.isNaN(t)) continue;
    s.t.push(t);
    s.tps.push(num(f[itps]));
    s.qps.push(num(f[iqps]));
    s.p99.push(num(f[ip99]));
    s.err.push(num(f[ierr]));
    s.reconn.push(num(f[irec]));
  }
}

function num(v: string | undefined): number {
  const n = v === undefined ? NaN : parseFloat(v);
  return Number.isNaN(n) ? 0 : n;
}

// ── engine-side PostgreSQL metrics (live sampler) ───────────────────────
// pg_timeseries.csv columns: t,active,total_conn,xacts_s,cache_hit_pct,wal_mb_s

export interface PgSeries {
  t: number[];
  active: number[];
  cacheHit: number[];
  walMbs: number[];
  xactsS: number[];
}

export function emptyPg(): PgSeries {
  return { t: [], active: [], cacheHit: [], walMbs: [], xactsS: [] };
}

export function appendPg(s: PgSeries, batch: SampleBatch): void {
  const cols = batch.header.split(",");
  const ix = (name: string) => cols.indexOf(name);
  const it = ix("t"), ia = ix("active"), ic = ix("cache_hit_pct"), iw = ix("wal_mb_s"), ix_ = ix("xacts_s");
  for (const line of batch.rows) {
    const f = line.split(",");
    const t = parseFloat(f[it]);
    if (Number.isNaN(t)) continue;
    s.t.push(t);
    s.active.push(num(f[ia]));
    s.cacheHit.push(num(f[ic]));
    s.walMbs.push(num(f[iw]));
    s.xactsS.push(num(f[ix_]));
  }
}
