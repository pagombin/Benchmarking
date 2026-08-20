import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { api } from "../api";
import type {
  AlertRow, ContDbMetrics, ContJob, ContSummary, ContTimeseries, MaintWindow, Me,
} from "../types";
import { ContChart } from "../components/ContChart";
import { fmtCompact, fmtNum, fmtWhen, relAge } from "../lib/format";
import { usePageTitle } from "../lib/ui";

// Continuous workload detail: preset window pills + custom range (URL-persisted
// so links are shareable), KPI band, charts with outage/maintenance shading,
// the outage ledger, alert history, and maintenance-window CRUD.

const WINDOWS = ["10m", "30m", "1h", "12h", "24h", "48h", "5d", "7d", "14d", "30d"];
const LIVE_WINDOWS = new Set(["10m", "30m", "1h"]);   // poll these every 10s

const C = {
  blue: "#4c9aff", green: "#2e8b57", amber: "#e0a93b", red: "#f85149",
  purple: "#9d6ade", grey: "#8b97a6",
};
const OUTAGE_FILL = "rgba(248,81,73,0.14)";
const LOAD_FILL = "rgba(224,169,59,0.12)";
const MAINT_FILL = "rgba(139,151,166,0.12)";

function fmtDur(s: number | null | undefined): string {
  if (s == null || !Number.isFinite(s)) return "—";
  if (s < 60) return `${Math.round(s)}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${String(Math.round(s % 60)).padStart(2, "0")}s`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ${String(Math.floor((s % 3600) / 60)).padStart(2, "0")}m`;
  return `${Math.floor(s / 86400)}d ${Math.floor((s % 86400) / 3600)}h`;
}

const epoch = (iso: string | null | undefined): number =>
  iso ? new Date(iso).getTime() / 1000 : NaN;

function Kpi({ label, value, title }: { label: string; value: string; title?: string }) {
  return (
    <div className="kpi" title={title}
         style={{ minWidth: 120, padding: "8px 14px" }}>
      <div className="subtle" style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: 0.5 }}>{label}</div>
      <div className="mono" style={{ fontSize: 20 }}>{value}</div>
    </div>
  );
}

export function ContinuousView({ me }: { me: Me }) {
  const { jobId } = useParams();
  const [params, setParams] = useSearchParams();
  const window_ = params.get("window") ?? "1h";
  const from = params.get("from") ?? "";
  const to = params.get("to") ?? "";
  const custom = !!from;

  const [job, setJob] = useState<ContJob | null>(null);
  const [ts, setTs] = useState<ContTimeseries | null>(null);
  const [summary, setSummary] = useState<ContSummary | null>(null);
  const [db, setDb] = useState<ContDbMetrics | null>(null);
  const [alerts, setAlerts] = useState<AlertRow[] | null>(null);
  const [maint, setMaint] = useState<MaintWindow[] | null>(null);
  const [sevFilter, setSevFilter] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [mwForm, setMwForm] = useState({ starts: "", ends: "", note: "", global: false });
  const canOp = me.role === "operator" || me.role === "admin";
  usePageTitle(job ? `${job.target_name ?? job.target_host ?? `job ${job.id}`} · Continuous` : "Continuous");

  const qs = custom
    ? `from=${encodeURIComponent(from)}${to ? `&to=${encodeURIComponent(to)}` : ""}`
    : `window=${window_}`;

  const load = useCallback(async () => {
    try {
      const base = `/api/continuous/${jobId}`;
      const [j, t, s, d, m] = await Promise.all([
        api.get<ContJob>(base),
        api.get<ContTimeseries>(`${base}/timeseries?${qs}`),
        api.get<ContSummary>(`${base}/summary?${qs}`),
        api.get<ContDbMetrics>(`${base}/dbmetrics?${qs}`),
        api.get<MaintWindow[]>(`/api/maintenance?job_id=${jobId}`),
      ]);
      setJob(j); setTs(t); setSummary(s); setDb(d); setMaint(m);
      setErr(null);
    } catch (e) { setErr((e as Error).message); }
  }, [jobId, qs]);

  const loadAlerts = useCallback(async () => {
    try {
      setAlerts(await api.get<AlertRow[]>(
        `/api/continuous/${jobId}/alerts?limit=200${sevFilter ? `&severity=${sevFilter}` : ""}`));
    } catch { /* the ledger card shows its own empty state */ }
  }, [jobId, sevFilter]);

  useEffect(() => { load(); }, [load]);
  useEffect(() => { loadAlerts(); }, [loadAlerts]);
  useEffect(() => {
    const live = !custom && LIVE_WINDOWS.has(window_)
      && job != null && job.state === "running";
    if (!live) return;
    const t = setInterval(() => { load(); loadAlerts(); }, 10000);
    return () => clearInterval(t);
  }, [custom, window_, job, load, loadAlerts]);

  const bands = useMemo(() => {
    if (!ts) return [];
    const endFallback = epoch(ts.to_utc);
    return [
      ...ts.maintenance.map((m) => ({
        from: epoch(m.starts_utc), to: epoch(m.ends_utc),
        color: MAINT_FILL, label: "maintenance",
      })),
      ...ts.outages.map((o) => ({
        from: epoch(o.started_utc),
        to: o.ended_utc ? epoch(o.ended_utc) : endFallback,
        color: o.kind === "load" ? LOAD_FILL : OUTAGE_FILL,
        label: `${o.kind} outage`,
      })),
    ];
  }, [ts]);

  const markers = useMemo(() => (ts?.events ?? []).map((e) => ({
    t: epoch(e.ts_utc), label: e.label || e.type, color: C.amber,
  })), [ts]);

  const cacheHit = useMemo(() => {
    if (!db) return [];
    return db.t.map((_, i) => {
      const h = db.blks_hit_rate[i], r = db.blks_read_rate[i];
      if (h == null || r == null || h + r <= 0) return null;
      return (h / (h + r)) * 100;
    });
  }, [db]);

  function pick(w: string) { setParams({ window: w }); }
  function applyCustom(e: React.FormEvent) {
    e.preventDefault();
    const f = (document.getElementById("cont-from") as HTMLInputElement).value;
    const t = (document.getElementById("cont-to") as HTMLInputElement).value;
    if (!f) return;
    const iso = (v: string) => v.length === 16 ? `${v}:00Z` : v.endsWith("Z") ? v : `${v}Z`;
    setParams(t ? { from: iso(f), to: iso(t) } : { from: iso(f) });
  }

  async function stop() {
    if (!job) return;
    if (!confirm(`Stop the 24/7 workload against “${job.target_name ?? job.target_host}”?\n`
      + "Historical metrics are retained; the workload can be resumed later.")) return;
    try { await api.post(`/api/continuous/${job.id}/stop`); await load(); }
    catch (ex) { alert((ex as Error).message); }
  }
  async function resume() {
    if (!job) return;
    try { await api.post(`/api/continuous/${job.id}/resume`); await load(); }
    catch (ex) { alert((ex as Error).message); }
  }
  async function addMaint(e: React.FormEvent) {
    e.preventDefault();
    try {
      const iso = (v: string) => v.length === 16 ? `${v}:00Z` : v;
      await api.post("/api/maintenance", {
        job_id: mwForm.global ? null : Number(jobId),
        starts_utc: iso(mwForm.starts), ends_utc: iso(mwForm.ends),
        note: mwForm.note,
      });
      setMwForm({ starts: "", ends: "", note: "", global: false });
      await load();
    } catch (ex) { alert((ex as Error).message); }
  }
  async function delMaint(id: number) {
    if (!confirm("Delete this maintenance window? Future outages in this span "
      + "will alert normally again.")) return;
    try { await api.del(`/api/maintenance/${id}`); await load(); }
    catch (ex) { alert((ex as Error).message); }
  }

  // Report-window export: a self-contained HTML snapshot of the KPI band and
  // the outage ledger for the selected window — the artifact you paste into a
  // ticket or an SLA review, generated client-side from data already loaded.
  function exportReport() {
    if (!job || !summary || !ts) return;
    const e2 = (v: unknown) => String(v ?? "")
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    const name = job.target_name ?? job.target_host ?? `job ${job.id}`;
    const su = summary;
    const kpis: [string, string][] = [
      ["Uptime", su.uptime_pct == null ? "—" : `${fmtNum(su.uptime_pct, su.uptime_pct === 100 ? 0 : 3)}%`],
      ["Downtime", fmtDur(su.downtime_s)],
      ["Unplanned outages", String(su.outages_unplanned_db)],
      ["Planned outages", String(su.outages_planned)],
      ["Load-gap outages", String(su.outages_load)],
      ["MTBF", fmtDur(su.mtbf_s)],
      ["MTTR", fmtDur(su.mttr_s)],
      ["Longest outage", fmtDur(su.longest_outage_s)],
      ["Avg TPS", su.tps_avg == null ? "—" : fmtNum(su.tps_avg, 0)],
      ["Worst p99", su.lat_p99_max == null ? "—" : `${fmtNum(su.lat_p99_max, 0)} ms`],
      ["Errors", fmtNum(su.errors_total, 0)],
      ["Harness relaunches", String(su.harness_relaunches)],
    ];
    const outRows = ts.outages.map((o) =>
      `<tr><td>${e2(o.kind)}${o.planned ? " (planned)" : ""}</td>`
      + `<td>${e2(o.started_utc)}</td><td>${e2(o.ended_utc ?? "ongoing")}</td>`
      + `<td class="n">${e2(fmtDur(o.duration_s))}</td>`
      + `<td>${e2(o.error_class ?? "")}</td><td>${e2(o.first_error ?? "")}</td></tr>`).join("\n")
      || '<tr><td colspan="6">No outages in this window.</td></tr>';
    const html = `<!doctype html><html><head><meta charset="utf-8">
<title>Availability report — ${e2(name)}</title>
<style>
 body{font:14px/1.5 -apple-system,Segoe UI,sans-serif;color:#1a2230;max-width:880px;margin:32px auto;padding:0 16px}
 h1{font-size:20px;margin:0 0 2px} .sub{color:#5a6676;font-size:12.5px;margin-bottom:18px}
 table{border-collapse:collapse;width:100%;margin:10px 0 22px}
 th,td{border:1px solid #d7dde6;padding:6px 9px;text-align:left;font-size:13px;vertical-align:top}
 th{background:#f2f5f9} td.n{text-align:right;font-variant-numeric:tabular-nums}
 .kpis td:first-child{color:#5a6676;width:190px}
</style></head><body>
<h1>Availability report — ${e2(name)}</h1>
<div class="sub">${e2(job.workload_type)} · ${e2(job.threads)} threads · run ${e2(job.run_id ?? "—")}<br>
Window (UTC): ${e2(su.from_utc)} → ${e2(su.to_utc)} (${fmtDur(su.window_s)})<br>
Generated ${e2(new Date().toISOString().slice(0, 19))}Z · uptime counts unplanned read/write
outages only; planned maintenance and load-generator gaps are listed but not charged.</div>
<table class="kpis"><tbody>
${kpis.map(([k, v]) => `<tr><td>${e2(k)}</td><td>${e2(v)}</td></tr>`).join("\n")}
</tbody></table>
<h1 style="font-size:16px">Outage ledger</h1>
<table><thead><tr><th>Kind</th><th>Started (UTC)</th><th>Ended</th><th>Duration</th>
<th>Class</th><th>First error</th></tr></thead><tbody>
${outRows}
</tbody></table>
</body></html>`;
    const blob = new Blob([html], { type: "text/html" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `availability-${name.replace(/[^a-zA-Z0-9._-]+/g, "_")}-${custom ? "custom" : window_}.html`;
    a.click();
    URL.revokeObjectURL(a.href);
  }

  if (err && !job) {
    return <div className="banner-err" style={{ margin: 20 }}>{err}</div>;
  }
  if (!job) return <div className="subtle mono" style={{ padding: 20 }}>loading…</div>;

  const active = ["queued", "running", "canceling"].includes(job.state);
  const s = summary;

  return (
    <>
      <div className="toolbar">
        <h1>
          <Link to="/continuous" className="subtle">Continuous</Link>
          {" / "}{job.target_name ?? job.target_host ?? `job ${job.id}`}
        </h1>
        <span className={`badge ${job.state === "running" ? "complete" : job.state === "failed" ? "failed" : job.state}`}>{job.state}</span>
        {job.supervisor?.load_stopped && <span className="chip">load stopped</span>}
        <div className="spacer" />
        {canOp && active && <button onClick={stop}>Stop workload</button>}
        {canOp && !active && job.desired_state !== "running" &&
          <button className="primary" onClick={resume}>Resume</button>}
      </div>
      <p className="subtle" style={{ marginTop: -8, marginBottom: 12, fontSize: 12.5 }}>
        {job.workload_type} · {job.threads} threads · run <span className="mono">{job.run_id ?? "—"}</span>
        {job.supervisor && <> · supervisor {job.supervisor.status ?? "?"} ·
          seg {job.supervisor.seg ?? "—"} · {job.supervisor.relaunches ?? 0} sysbench relaunch(es)</>}
        {job.last_sample_utc && <> · last sample {relAge(job.last_sample_utc)}</>}
      </p>
      {err && <div className="banner-err">{err}</div>}

      <div className="toolbar" style={{ gap: 6, flexWrap: "wrap" }}>
        {WINDOWS.map((w) => (
          <button key={w} className={!custom && window_ === w ? "primary btn-sm" : "btn-sm"}
                  onClick={() => pick(w)}>{w}</button>
        ))}
        <form onSubmit={applyCustom} style={{ display: "inline-flex", gap: 6, alignItems: "center" }}>
          <input id="cont-from" type="datetime-local" defaultValue={from.slice(0, 16)}
                 title="from (UTC)" style={{ width: 190 }} />
          <span className="subtle">→</span>
          <input id="cont-to" type="datetime-local" defaultValue={to.slice(0, 16)}
                 title="to (UTC, empty = now)" style={{ width: 190 }} />
          <button className="btn-sm" type="submit">custom (UTC, ≤30d)</button>
        </form>
        <div className="spacer" />
        <button className="btn-sm" onClick={exportReport} disabled={!summary || !ts}
                title="Self-contained HTML snapshot of the KPIs + outage ledger for this window">
          Export report
        </button>
        {job.run_id && (
          <a className="btn-sm" style={{ textDecoration: "none" }}
             href={`/runs/${job.run_id}/csv?which=continuous`}
             title="Raw per-second samples for the whole run (CSV, can be large)">
            Raw CSV
          </a>
        )}
      </div>

      {s && (
        <div className="row" style={{ gap: 10, flexWrap: "wrap", margin: "10px 0 14px" }}>
          <Kpi label="uptime" value={s.uptime_pct == null ? "—" : `${fmtNum(s.uptime_pct, s.uptime_pct === 100 ? 0 : 3)}%`}
               title="window minus unplanned db outages (read/write probes)" />
          <Kpi label="downtime" value={fmtDur(s.downtime_s)} />
          <Kpi label="outages" value={`${s.outages_unplanned_db}${s.outages_planned ? ` (+${s.outages_planned} planned)` : ""}`} />
          <Kpi label="MTBF" value={fmtDur(s.mtbf_s)} />
          <Kpi label="MTTR" value={fmtDur(s.mttr_s)} />
          <Kpi label="longest outage" value={fmtDur(s.longest_outage_s)} />
          <Kpi label="avg TPS" value={s.tps_avg == null ? "—" : fmtNum(s.tps_avg, 0)} />
          <Kpi label="worst p99" value={s.lat_p99_max == null ? "—" : `${fmtNum(s.lat_p99_max, 0)}ms`} />
          <Kpi label="errors" value={fmtNum(s.errors_total, 0)} />
          <Kpi label="relaunches" value={String(s.harness_relaunches)} title="harness relaunches (reboot/crash recovery)" />
        </div>
      )}

      {ts && ts.t.length === 0 && (
        <div className="card"><div className="empty">
          No samples in this window yet{active ? " — the workload may still be starting." : "."}
        </div></div>
      )}
      {ts && ts.t.length > 0 && (
        <>
          <div className="card">
            <ContChart title={`Throughput (TPS) — ${ts.resolution} resolution, outages shaded`}
                       xs={ts.t}
                       series={[
                         { label: "tps avg", values: ts.tps_avg, stroke: C.blue },
                         { label: "tps min", values: ts.tps_min, stroke: C.grey },
                       ]}
                       height={260} yFormat={fmtCompact} bands={bands} markers={markers} />
          </div>
          <div className="grid2">
            <div className="card">
              <ContChart title="p99 latency (ms)" xs={ts.t}
                         series={[
                           { label: "p99 avg", values: ts.lat_p99_avg, stroke: C.purple },
                           { label: "p99 max", values: ts.lat_p99_max, stroke: C.red },
                         ]}
                         yFormat={fmtCompact} bands={bands} />
            </div>
            <div className="card">
              <ContChart title="errors + reconnects /interval" xs={ts.t}
                         series={[
                           { label: "errors", values: ts.err_sum, stroke: C.red },
                           { label: "reconnects", values: ts.reconn_sum, stroke: C.amber },
                         ]}
                         yFormat={fmtCompact} bands={bands} />
            </div>
          </div>
          {db && db.t.length > 0 && (
            <div className="grid2">
              <div className="card">
                <ContChart title="connections" xs={db.t} spanGaps
                           series={[
                             { label: "active", values: db.conn_active, stroke: C.blue },
                             { label: "total", values: db.conn_total, stroke: C.grey },
                           ]} yFormat={fmtCompact} />
              </div>
              <div className="card">
                <ContChart title="cache hit % / commits per s" xs={db.t} spanGaps
                           series={[
                             { label: "cache hit %", values: cacheHit, stroke: C.green },
                             { label: "commits/s", values: db.xact_commit_rate, stroke: C.blue, scale: "y2" },
                           ]} yFormat={fmtCompact} />
              </div>
              <div className="card">
                <ContChart title="WAL MB/s / archiver failures" xs={db.t} spanGaps
                           series={[
                             { label: "WAL MB/s",
                               values: db.wal_bytes_rate.map((v) => (v == null ? null : v / 1048576)),
                               stroke: C.purple },
                             { label: "archive failed /s", values: db.archive_failed_rate,
                               stroke: C.red, scale: "y2" },
                           ]} yFormat={(v) => fmtNum(v, 2)} />
              </div>
              <div className="card">
                <ContChart title="DB size (GB) / replication lag (s)" xs={db.t} spanGaps
                           series={[
                             { label: "db size GB",
                               values: db.db_size.map((v) => (v == null ? null : v / 1073741824)),
                               stroke: C.grey },
                             { label: "repl lag s", values: db.repl_lag_s, stroke: C.amber, scale: "y2" },
                           ]} yFormat={(v) => fmtNum(v, 2)} />
              </div>
            </div>
          )}
        </>
      )}

      <div className="card">
        <div className="card-head"><h2>Outage ledger</h2>
          <span className="subtle" style={{ fontSize: 12 }}>
            read/write = database unreachable after retries; load = load generator stalled while probes stayed green
          </span>
        </div>
        <table>
          <thead><tr><th>Kind</th><th>Started (UTC)</th><th>Ended</th>
            <th className="num">Duration</th><th>Class</th><th>Planned</th><th>First error</th></tr></thead>
          <tbody>
            {!ts || ts.outages.length === 0 ? (
              <tr><td colSpan={7} className="empty">No outages in this window. 🎉</td></tr>
            ) : ts.outages.map((o) => (
              <tr key={o.id}>
                <td><span className={`badge ${o.kind === "load" ? "running" : "failed"}`}>{o.kind}</span></td>
                <td className="mono" style={{ fontSize: 12 }}>{o.started_utc}</td>
                <td className="mono" style={{ fontSize: 12 }}>{o.ended_utc ?? <b>ongoing</b>}</td>
                <td className="num">{fmtDur(o.duration_s)}</td>
                <td>{o.error_class || "—"}</td>
                <td>{o.planned ? "yes" : ""}</td>
                <td className="subtle" style={{ maxWidth: 320, fontSize: 12 }}>{o.first_error || ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="card">
        <div className="card-head"><h2>Alert history</h2>
          <div className="spacer" />
          <select value={sevFilter} onChange={(e) => setSevFilter(e.target.value)} style={{ width: 130 }}>
            <option value="">all severities</option>
            <option value="crit">crit</option>
            <option value="warn">warn</option>
            <option value="info">info</option>
          </select>
        </div>
        <table>
          <thead><tr><th>Sev</th><th>Type</th><th>Fired (UTC)</th><th>Resolved</th>
            <th>Delivery</th><th>Detail</th></tr></thead>
          <tbody>
            {alerts === null ? <tr><td colSpan={6} className="empty mono">loading…</td></tr>
              : alerts.length === 0 ? <tr><td colSpan={6} className="empty">No alerts recorded.</td></tr>
                : alerts.map((a) => {
                  let detail = "";
                  try {
                    const c = JSON.parse(a.context || "{}");
                    detail = c.detail || c.first_error || c.reason || "";
                  } catch { /* raw context is optional */ }
                  return (
                    <tr key={a.id}>
                      <td><span className={`badge ${a.severity === "crit" ? "failed" : a.severity === "warn" ? "running" : "complete"}`}>{a.severity}</span></td>
                      <td className="mono" style={{ fontSize: 12 }}>{a.type}</td>
                      <td className="mono subtle" style={{ fontSize: 12 }} title={a.fired_utc}>{fmtWhen(a.fired_utc)}</td>
                      <td className="mono subtle" style={{ fontSize: 12 }}>{a.resolved_utc ? fmtWhen(a.resolved_utc) : "open"}</td>
                      <td className="mono" style={{ fontSize: 12 }}
                          title={`${a.delivery_attempts} attempt(s)`}>
                        {a.delivery ?? "pending"}
                      </td>
                      <td className="subtle" style={{ maxWidth: 340, fontSize: 12 }}>{detail}</td>
                    </tr>
                  );
                })}
          </tbody>
        </table>
      </div>

      <div className="card">
        <div className="card-head"><h2>Maintenance windows</h2>
          <span className="subtle" style={{ fontSize: 12 }}>
            outages inside a window are marked planned and their alerts are stored but not sent
          </span>
        </div>
        <table>
          <thead><tr><th>Scope</th><th>Starts (UTC)</th><th>Ends (UTC)</th><th>Note</th><th></th></tr></thead>
          <tbody>
            {maint === null ? <tr><td colSpan={5} className="empty mono">loading…</td></tr>
              : maint.length === 0 ? <tr><td colSpan={5} className="empty">No maintenance windows.</td></tr>
                : maint.map((m) => (
                  <tr key={m.id}>
                    <td>{m.job_id == null ? <span className="chip">global</span> : "this workload"}</td>
                    <td className="mono" style={{ fontSize: 12 }}>{m.starts_utc}</td>
                    <td className="mono" style={{ fontSize: 12 }}>{m.ends_utc}</td>
                    <td className="subtle">{m.note || ""}</td>
                    <td className="row-actions">
                      {canOp && <button className="btn-sm" onClick={() => delMaint(m.id)}>Delete</button>}
                    </td>
                  </tr>
                ))}
          </tbody>
        </table>
        {canOp && (
          <form onSubmit={addMaint} className="row" style={{ marginTop: 10, alignItems: "flex-end", flexWrap: "wrap" }}>
            <div className="field" style={{ maxWidth: 200 }}>
              <label>Starts (UTC)</label>
              <input type="datetime-local" required value={mwForm.starts}
                     onChange={(e) => setMwForm({ ...mwForm, starts: e.target.value })} />
            </div>
            <div className="field" style={{ maxWidth: 200 }}>
              <label>Ends (UTC)</label>
              <input type="datetime-local" required value={mwForm.ends}
                     onChange={(e) => setMwForm({ ...mwForm, ends: e.target.value })} />
            </div>
            <div className="field">
              <label>Note</label>
              <input value={mwForm.note} placeholder="planned failover drill"
                     onChange={(e) => setMwForm({ ...mwForm, note: e.target.value })} />
            </div>
            <label className="follow" style={{ marginBottom: 8 }}>
              <input type="checkbox" checked={mwForm.global}
                     onChange={(e) => setMwForm({ ...mwForm, global: e.target.checked })} />
              {" "}global (all workloads)
            </label>
            <button className="btn-sm" type="submit" style={{ marginBottom: 8 }}>Add window</button>
          </form>
        )}
      </div>
    </>
  );
}
