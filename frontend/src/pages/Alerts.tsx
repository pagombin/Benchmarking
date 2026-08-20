import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import type { AlertRow, ContJob } from "../types";
import { fmtWhen, relAge } from "../lib/format";
import { exportCsv, usePageTitle } from "../lib/ui";

// Global alerting console: every stored alert across every continuous
// workload — filterable, with the "how often do these fire" frequency
// summary (count + MTTR per type over the fetched window).

const SINCE = [
  { v: "", label: "all history (35d retention)" },
  { v: "24h", label: "last 24h" },
  { v: "7d", label: "last 7 days" },
  { v: "30d", label: "last 30 days" },
];
const SINCE_S: Record<string, number> = { "24h": 86400, "7d": 604800, "30d": 2592000 };

function sinceIso(v: string): string {
  if (!v) return "";
  return new Date(Date.now() - SINCE_S[v] * 1000).toISOString().slice(0, 19) + "Z";
}

function durS(a: string, b: string | null): number | null {
  if (!b) return null;
  const s = (new Date(b).getTime() - new Date(a).getTime()) / 1000;
  return Number.isFinite(s) && s >= 0 ? s : null;
}

function fmtDur(s: number | null): string {
  if (s === null) return "—";
  if (s < 60) return `${Math.round(s)}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${String(Math.round(s % 60)).padStart(2, "0")}s`;
  return `${Math.floor(s / 3600)}h ${String(Math.floor((s % 3600) / 60)).padStart(2, "0")}m`;
}

export function Alerts() {
  usePageTitle("Alerts");
  const [rows, setRows] = useState<AlertRow[] | null>(null);
  const [jobs, setJobs] = useState<ContJob[]>([]);
  const [severity, setSeverity] = useState("");
  const [type, setType] = useState("");
  const [since, setSince] = useState("7d");
  const [openOnly, setOpenOnly] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function load() {
    try {
      const qs = new URLSearchParams();
      if (severity) qs.set("severity", severity);
      if (type) qs.set("type", type);
      if (since) qs.set("since", sinceIso(since));
      if (openOnly) qs.set("unresolved", "1");
      qs.set("limit", "1000");
      setRows(await api.get<AlertRow[]>(`/api/alerts?${qs}`));
      setErr(null);
    } catch (e) { setErr((e as Error).message); }
  }
  useEffect(() => { load(); /* eslint-disable-next-line */ }, [severity, type, since, openOnly]);
  useEffect(() => { api.get<ContJob[]>("/api/continuous").then(setJobs).catch(() => {}); }, []);

  const jobName = useMemo(() => {
    const m = new Map<number, string>();
    jobs.forEach((j) => m.set(j.id, j.target_name ?? j.target_host ?? `job ${j.id}`));
    return (id: number | null) => (id === null ? "loadgen" : m.get(id) ?? `job ${id}`);
  }, [jobs]);

  const types = useMemo(
    () => Array.from(new Set((rows ?? []).map((r) => r.type))).sort(), [rows]);

  // "db_unreachable: 14 in 7d, MTTR 41s" — the frequency summary.
  const freq = useMemo(() => {
    const by = new Map<string, { n: number; open: number; durs: number[] }>();
    for (const r of rows ?? []) {
      const e = by.get(r.type) ?? { n: 0, open: 0, durs: [] };
      e.n += 1;
      if (!r.resolved_utc) e.open += 1;
      const d = durS(r.fired_utc, r.resolved_utc);
      if (d !== null && d > 0) e.durs.push(d);
      by.set(r.type, e);
    }
    return Array.from(by.entries())
      .map(([t, e]) => ({
        type: t, n: e.n, open: e.open,
        mttr: e.durs.length ? e.durs.reduce((a, b) => a + b, 0) / e.durs.length : null,
      }))
      .sort((a, b) => b.n - a.n);
  }, [rows]);

  function doExport() {
    exportCsv(`alerts-${since || "all"}.csv`,
      ["id", "target", "type", "severity", "fired_utc", "resolved_utc",
       "delivery", "delivery_attempts", "context"],
      (rows ?? []).map((r) => [r.id, jobName(r.job_id), r.type, r.severity,
        r.fired_utc, r.resolved_utc, r.delivery, r.delivery_attempts, r.context]));
  }

  return (
    <>
      <div className="toolbar"><h1>Alerts</h1><div className="spacer" />
        <select value={since} onChange={(e) => setSince(e.target.value)} style={{ width: 190 }}>
          {SINCE.map((s) => <option key={s.v} value={s.v}>{s.label}</option>)}
        </select>
        <select value={severity} onChange={(e) => setSeverity(e.target.value)} style={{ width: 120 }}>
          <option value="">all severities</option>
          <option value="crit">crit</option><option value="warn">warn</option>
          <option value="info">info</option>
        </select>
        <select value={type} onChange={(e) => setType(e.target.value)} style={{ width: 170 }}>
          <option value="">all types</option>
          {types.map((t) => <option key={t} value={t}>{t}</option>)}
        </select>
        <label className="follow" style={{ margin: 0 }}>
          <input type="checkbox" checked={openOnly} onChange={(e) => setOpenOnly(e.target.checked)} /> open only
        </label>
        <button className="btn-sm" onClick={doExport} disabled={!rows?.length}>Export CSV</button>
      </div>
      <p className="subtle" style={{ marginTop: -8, marginBottom: 14, fontSize: 12.5 }}>
        Every alert is stored here first — whether or not Slack delivery succeeded — and
        deduplicated while unresolved. Suppressed = inside a maintenance window. Times are UTC.
      </p>
      {err && <div className="banner-err">{err} <button className="btn-sm" onClick={load}>Retry</button></div>}

      {rows !== null && rows.length > 0 && (
        <div className="card">
          <div className="card-head"><h2>Frequency ({since || "all"})</h2></div>
          <table>
            <thead><tr><th>Type</th><th className="num">Fired</th>
              <th className="num">Open now</th><th className="num">Mean time to resolve</th></tr></thead>
            <tbody>
              {freq.map((f) => (
                <tr key={f.type}>
                  <td className="mono">{f.type}</td>
                  <td className="num">{f.n}</td>
                  <td className="num">{f.open || ""}</td>
                  <td className="num">{fmtDur(f.mttr)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div className="card">
        <div className="card-head"><h2>History</h2>
          <span className="subtle" style={{ fontSize: 12 }}>
            {rows ? `${rows.length} row(s)` : ""}</span></div>
        <table>
          <thead><tr><th>Sev</th><th>Target</th><th>Type</th><th>Fired (UTC)</th>
            <th>Resolved</th><th>Delivery</th><th>Detail</th></tr></thead>
          <tbody>
            {rows === null ? (
              <tr><td colSpan={7} className="empty mono">loading…</td></tr>
            ) : rows.length === 0 ? (
              <tr><td colSpan={7} className="empty">
                No alerts in this window — either a quiet platform or no{" "}
                <Link to="/continuous">continuous workload</Link> running.
              </td></tr>
            ) : rows.map((a) => {
              let detail = "";
              try {
                const c = JSON.parse(a.context || "{}");
                detail = c.detail || c.first_error || c.reason || "";
              } catch { /* context is best-effort JSON */ }
              return (
                <tr key={a.id}>
                  <td><span className={`badge ${a.severity === "crit" ? "failed" : a.severity === "warn" ? "running" : "complete"}`}>{a.severity}</span></td>
                  <td>{a.job_id !== null
                    ? <Link to={`/continuous/${a.job_id}`}>{jobName(a.job_id)}</Link>
                    : <span className="subtle">loadgen</span>}</td>
                  <td className="mono" style={{ fontSize: 12 }}>{a.type}</td>
                  <td className="mono subtle" style={{ fontSize: 12 }} title={a.fired_utc}>
                    {fmtWhen(a.fired_utc)} <span className="subtle">({relAge(a.fired_utc)})</span></td>
                  <td className="mono subtle" style={{ fontSize: 12 }}>
                    {a.resolved_utc ? fmtWhen(a.resolved_utc) : <b>open</b>}</td>
                  <td className="mono" style={{ fontSize: 12 }}
                      title={`${a.delivery_attempts} attempt(s)`}>
                    {a.delivery === "slack:ok" ? "delivered"
                      : a.delivery === "slack:failed" ? `failed ×${a.delivery_attempts}`
                        : a.delivery ?? "pending"}</td>
                  <td className="subtle" style={{ maxWidth: 340, fontSize: 12 }}>{detail}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </>
  );
}
