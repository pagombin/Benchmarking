import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import type { ContJob, Me, Target } from "../types";
import { fmtNum, relAge } from "../lib/format";
import { usePageTitle } from "../lib/ui";

// Continuous workloads: always-on 24/7 load against a saved target — the
// "experience the platform like a real customer" board. Start/stop/resume
// here; the detail page holds the historical metrics and the outage ledger.

const WORKLOADS = [
  { v: "oltp_read_write", label: "oltp_read_write — mixed 70/30 OLTP" },
  { v: "oltp_read_only", label: "oltp_read_only — pure reads" },
  { v: "oltp_write_only", label: "oltp_write_only — pure writes" },
  { v: "tpcc", label: "tpcc — warehouse OLTP (needs prepared TPC-C data)" },
];

const BLANK = {
  target_id: 0, workload_type: "oltp_read_write", threads: 16, tables: 8,
  table_size: 100000, scale: 10, label: "", prepare: true,
};

function Spark({ spark }: { spark?: { t: number[]; tps: (number | null)[] } }) {
  if (!spark || spark.t.length < 2) return <span className="subtle mono">—</span>;
  const vals = spark.tps.map((v) => (v == null || !Number.isFinite(v) ? 0 : v));
  const max = Math.max(...vals, 1);
  const w = 120, h = 26;
  const pts = vals.map((v, i) =>
    `${(i / (vals.length - 1)) * w},${h - (v / max) * (h - 2) - 1}`).join(" ");
  return (
    <svg width={w} height={h} aria-label="TPS, last 30 minutes">
      <polyline points={pts} fill="none" stroke="#4c9aff" strokeWidth="1.4" />
    </svg>
  );
}

function healthDot(j: ContJob): { cls: string; title: string } {
  if (j.state !== "running") return { cls: "subtle", title: j.state };
  if (j.open_outages.some((o) => o.kind !== "load" && !o.planned)) {
    return { cls: "crit", title: "database outage open" };
  }
  if (j.open_outages.length || j.open_alerts.some((a) => a.severity !== "info")) {
    return { cls: "warn", title: "degraded (open alert/outage)" };
  }
  return { cls: "ok", title: "healthy" };
}

export function Continuous({ me }: { me: Me }) {
  usePageTitle("Continuous");
  const [jobs, setJobs] = useState<ContJob[] | null>(null);
  const [targets, setTargets] = useState<Target[]>([]);
  const [form, setForm] = useState({ ...BLANK });
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const canOp = me.role === "operator" || me.role === "admin";

  async function load() {
    try {
      setJobs(await api.get<ContJob[]>("/api/continuous"));
      setErr(null);
    } catch (e) { setErr((e as Error).message); }
  }
  useEffect(() => {
    load();
    api.get<Target[]>("/api/targets").then(setTargets).catch(() => {});
    const t = setInterval(load, 10000);
    return () => clearInterval(t);
  }, []);

  const activeTargets = useMemo(() => new Set(
    (jobs ?? []).filter((j) =>
      ["queued", "running", "canceling"].includes(j.state) || j.desired_state === "running")
      .map((j) => j.target_id)), [jobs]);

  async function start(e: React.FormEvent) {
    e.preventDefault();
    setErr(null);
    setBusy(true);
    try {
      await api.post("/api/continuous", form);
      setForm({ ...BLANK });
      await load();
    } catch (ex) { setErr((ex as Error).message); } finally { setBusy(false); }
  }

  async function stop(j: ContJob) {
    if (!confirm(`Stop the 24/7 workload against “${j.target_name ?? j.target_host}”?\n`
      + "Historical metrics, the outage ledger and alert history are retained; "
      + "the workload can be resumed later (same timeline).")) return;
    try { await api.post(`/api/continuous/${j.id}/stop`); await load(); }
    catch (ex) { alert((ex as Error).message); }
  }

  async function resume(j: ContJob) {
    try { await api.post(`/api/continuous/${j.id}/resume`); await load(); }
    catch (ex) { alert((ex as Error).message); }
  }

  const set = (k: keyof typeof form) =>
    (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) => {
      const v = e.target.type === "checkbox"
        ? (e.target as HTMLInputElement).checked
        : ["threads", "tables", "table_size", "scale", "target_id"].includes(k)
          ? Number(e.target.value) : e.target.value;
      setForm({ ...form, [k]: v });
    };

  return (
    <>
      <div className="toolbar"><h1>Continuous</h1></div>
      <p className="subtle" style={{ marginTop: -8, marginBottom: 16 }}>
        Always-on workloads that run <b>until explicitly stopped</b> — surviving sysbench crashes
        (supervisor relaunch with backoff), worker restarts, and full droplet reboots
        (desired-state reconcile). Each maintains 30 days of queryable metrics, a downtime
        ledger, and Slack alerting. Times are UTC.
      </p>
      {err && <div className="banner-err">{err}</div>}

      <div className="card">
        <div className="card-head"><h2>Workloads</h2></div>
        <table>
          <thead><tr>
            <th></th><th>Target</th><th>Workload</th><th className="num">Threads</th>
            <th>State</th><th className="num">Uptime 24h</th><th className="num">TPS 24h</th>
            <th>TPS (30m)</th><th>Last sample</th><th>Outage</th><th>Last alert</th><th></th>
          </tr></thead>
          <tbody>
            {jobs === null ? (
              <tr><td colSpan={12} className="empty mono">loading…</td></tr>
            ) : jobs.length === 0 ? (
              <tr><td colSpan={12} className="empty">
                No continuous workloads yet{canOp ? " — start one below." : "."}
              </td></tr>
            ) : jobs.map((j) => {
              const dot = healthDot(j);
              const openOutage = j.open_outages[0];
              const lastAlert = j.open_alerts[0];
              const active = ["queued", "running", "canceling"].includes(j.state);
              return (
                <tr key={j.id}>
                  <td title={dot.title}>
                    <span className={`badge ${dot.cls === "ok" ? "complete" : dot.cls === "crit" ? "failed" : dot.cls === "warn" ? "running" : "canceled"}`}>●</span>
                  </td>
                  <td><Link to={`/continuous/${j.id}`}>{j.target_name ?? j.target_host ?? `job ${j.id}`}</Link></td>
                  <td className="mono" style={{ fontSize: 12 }}>{j.workload_type}</td>
                  <td className="num">{j.threads ?? "—"}</td>
                  <td>
                    <span className={`badge ${j.state === "running" ? "complete" : j.state === "failed" ? "failed" : j.state}`}>{j.state}</span>
                    {j.desired_state === "running" && !active &&
                      <span className="chip" title="desired running — the worker will relaunch it">relaunching</span>}
                    {j.supervisor?.load_stopped &&
                      <span className="chip" title="the supervisor stopped launching sysbench after repeated instant failures">load stopped</span>}
                  </td>
                  <td className="num">{j.uptime_24h_pct == null ? "—" : `${fmtNum(j.uptime_24h_pct, 2)}%`}</td>
                  <td className="num">{j.tps_avg_24h == null ? "—" : fmtNum(j.tps_avg_24h, 0)}</td>
                  <td><Spark spark={j.spark} /></td>
                  <td className="mono subtle" title={j.last_sample_utc}>
                    {j.last_sample_utc ? relAge(j.last_sample_utc) : "—"}
                  </td>
                  <td>
                    {openOutage
                      ? <span className="badge failed" title={`${openOutage.kind} outage since ${openOutage.started_utc}`}>
                          {openOutage.kind} · {relAge(openOutage.started_utc)}
                        </span>
                      : <span className="subtle">—</span>}
                  </td>
                  <td>
                    {lastAlert
                      ? <span className={`badge ${lastAlert.severity === "crit" ? "failed" : "running"}`}
                              title={`${lastAlert.type} · fired ${lastAlert.fired_utc} (open)`}>
                          {lastAlert.type} · {relAge(lastAlert.fired_utc)}
                        </span>
                      : <span className="subtle">—</span>}
                  </td>
                  <td className="row-actions">
                    {canOp && active && <button className="btn-sm" onClick={() => stop(j)}>Stop</button>}
                    {canOp && !active && j.desired_state !== "running" &&
                      <button className="btn-sm" onClick={() => resume(j)}>Resume</button>}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {canOp && (
        <div className="card">
          <div className="card-head"><h2>Start a continuous workload</h2></div>
          <p className="subtle" style={{ marginTop: -6, marginBottom: 10, fontSize: 12.5 }}>
            Requires a <Link to="/targets">saved target</Link> with a stored password — the
            credential must survive reboots so the workload can relaunch itself. One continuous
            workload per target.
          </p>
          <form onSubmit={start}>
            <div className="row">
              <div className="field">
                <label>Saved target</label>
                <select value={form.target_id} onChange={set("target_id")} required>
                  <option value={0} disabled>choose a target…</option>
                  {targets.map((t) => (
                    <option key={t.id} value={t.id} disabled={activeTargets.has(t.id)}>
                      {t.name} ({t.host}){activeTargets.has(t.id) ? " — busy" : ""}
                    </option>
                  ))}
                </select>
              </div>
              <div className="field">
                <label>Workload</label>
                <select value={form.workload_type} onChange={set("workload_type")}>
                  {WORKLOADS.map((w) => <option key={w.v} value={w.v}>{w.label}</option>)}
                </select>
              </div>
              <div className="field" style={{ maxWidth: 110 }}>
                <label>Threads</label>
                <input type="number" min={1} max={512} value={form.threads} onChange={set("threads")} />
              </div>
            </div>
            <div className="row">
              <div className="field" style={{ maxWidth: 110 }}>
                <label>Tables</label>
                <input type="number" min={1} value={form.tables} onChange={set("tables")} />
              </div>
              {form.workload_type === "tpcc" ? (
                <div className="field" style={{ maxWidth: 130 }}>
                  <label>Scale (warehouses)</label>
                  <input type="number" min={1} value={form.scale} onChange={set("scale")} />
                </div>
              ) : (
                <div className="field" style={{ maxWidth: 150 }}>
                  <label>Rows per table</label>
                  <input type="number" min={100} value={form.table_size} onChange={set("table_size")} />
                </div>
              )}
              <div className="field">
                <label>Label (optional)</label>
                <input value={form.label} onChange={set("label")} placeholder="247-advanced-nyc3" />
              </div>
            </div>
            <label className="follow">
              <input type="checkbox" checked={form.prepare} onChange={set("prepare")} />
              {" "}load the dataset first if it's missing (idempotent prepare)
            </label>
            <div style={{ marginTop: 10 }}>
              <button className="primary" disabled={busy || !form.target_id} type="submit">
                {busy ? "Starting…" : "Start workload"}
              </button>
            </div>
          </form>
        </div>
      )}
    </>
  );
}
