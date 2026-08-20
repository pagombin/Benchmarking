import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import type { ContJob, Outage } from "../types";
import { fmtWhen, relAge } from "../lib/format";
import { exportCsv, usePageTitle } from "../lib/ui";

// Global outage ledger: every read/write/load outage across every continuous
// workload, with per-target availability math over the selected window and
// the planned/unplanned split that SLA conversations actually need.

const SINCE = [
  { v: "24h", label: "last 24h", s: 86400 },
  { v: "7d", label: "last 7 days", s: 604800 },
  { v: "30d", label: "last 30 days", s: 2592000 },
  { v: "", label: "all history (35d retention)", s: 0 },
];

function fmtDur(s: number | null): string {
  if (s === null) return "—";
  if (s < 60) return `${Math.round(s)}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${String(Math.round(s % 60)).padStart(2, "0")}s`;
  return `${Math.floor(s / 3600)}h ${String(Math.floor((s % 3600) / 60)).padStart(2, "0")}m`;
}

/** Union length of [start,end) intervals clipped to [w0,w1], in seconds. */
function unionSeconds(ivs: [number, number][], w0: number, w1: number): number {
  const clipped = ivs
    .map(([a, b]) => [Math.max(a, w0), Math.min(b, w1)] as [number, number])
    .filter(([a, b]) => b > a)
    .sort((x, y) => x[0] - y[0]);
  let total = 0, curA = 0, curB = -1;
  for (const [a, b] of clipped) {
    if (a > curB) { total += Math.max(0, curB - curA); curA = a; curB = b; }
    else curB = Math.max(curB, b);
  }
  total += Math.max(0, curB - curA);
  return total / 1000;
}

export function Outages() {
  usePageTitle("Outages");
  const [rows, setRows] = useState<Outage[] | null>(null);
  const [jobs, setJobs] = useState<ContJob[]>([]);
  const [kind, setKind] = useState("");
  const [planned, setPlanned] = useState("");   // "" | "0" | "1"
  const [openOnly, setOpenOnly] = useState(false);
  const [since, setSince] = useState("7d");
  const [err, setErr] = useState<string | null>(null);
  const [now, setNow] = useState(() => Date.now());

  async function load() {
    try {
      const qs = new URLSearchParams();
      if (kind) qs.set("kind", kind);
      if (planned) qs.set("planned", planned);
      if (openOnly) qs.set("open_only", "1");
      const preset = SINCE.find((s) => s.v === since);
      if (preset?.s) {
        qs.set("since", new Date(Date.now() - preset.s * 1000).toISOString().slice(0, 19) + "Z");
      }
      qs.set("limit", "1000");
      setRows(await api.get<Outage[]>(`/api/outages?${qs}`));
      setNow(Date.now());
      setErr(null);
    } catch (e) { setErr((e as Error).message); }
  }
  useEffect(() => { load(); /* eslint-disable-next-line */ }, [kind, planned, openOnly, since]);
  useEffect(() => { api.get<ContJob[]>("/api/continuous").then(setJobs).catch(() => {}); }, []);

  const jobName = useMemo(() => {
    const m = new Map<number, string>();
    jobs.forEach((j) => m.set(j.id, j.target_name ?? j.target_host ?? `job ${j.id}`));
    return (id: number) => m.get(id) ?? `job ${id}`;
  }, [jobs]);

  // Per-target availability over the selected window: uptime uses the union
  // of *unplanned db* outage intervals (read/write) — load gaps and planned
  // maintenance count as downtime events but not against availability.
  const windowS = SINCE.find((s) => s.v === since)?.s ?? 0;
  const perTarget = useMemo(() => {
    if (!rows) return [];
    const w1 = now, w0 = windowS ? now - windowS * 1000 : 0;
    const by = new Map<number, { total: number; open: number; planned: number;
      unplanned: number; load: number; dbIvs: [number, number][]; longest: number }>();
    for (const o of rows) {
      const e = by.get(o.job_id) ?? { total: 0, open: 0, planned: 0,
        unplanned: 0, load: 0, dbIvs: [], longest: 0 };
      e.total += 1;
      if (!o.ended_utc) e.open += 1;
      if (o.planned) e.planned += 1;
      else if (o.kind === "load") e.load += 1;
      else {
        e.unplanned += 1;
        const a = new Date(o.started_utc).getTime();
        const b = o.ended_utc ? new Date(o.ended_utc).getTime() : w1;
        if (Number.isFinite(a) && b > a) {
          e.dbIvs.push([a, b]);
          e.longest = Math.max(e.longest, (Math.min(b, w1) - Math.max(a, w0)) / 1000);
        }
      }
      by.set(o.job_id, e);
    }
    return Array.from(by.entries()).map(([jid, e]) => {
      const down = windowS ? unionSeconds(e.dbIvs, w0, w1) : null;
      return {
        jid, ...e,
        downS: down,
        uptimePct: down !== null && windowS ? 100 * (1 - down / windowS) : null,
      };
    }).sort((a, b) => (a.uptimePct ?? 101) - (b.uptimePct ?? 101));
  }, [rows, windowS, now]);

  function doExport() {
    exportCsv(`outages-${since || "all"}.csv`,
      ["id", "target", "kind", "planned", "started_utc", "ended_utc",
       "duration_s", "error_class", "first_error"],
      (rows ?? []).map((o) => [o.id, jobName(o.job_id), o.kind, o.planned,
        o.started_utc, o.ended_utc, o.duration_s, o.error_class, o.first_error]));
  }

  return (
    <>
      <div className="toolbar"><h1>Outages</h1><div className="spacer" />
        <select value={since} onChange={(e) => setSince(e.target.value)} style={{ width: 190 }}>
          {SINCE.map((s) => <option key={s.v} value={s.v}>{s.label}</option>)}
        </select>
        <select value={kind} onChange={(e) => setKind(e.target.value)} style={{ width: 130 }}>
          <option value="">all kinds</option>
          <option value="read">read</option><option value="write">write</option>
          <option value="load">load</option>
        </select>
        <select value={planned} onChange={(e) => setPlanned(e.target.value)} style={{ width: 140 }}>
          <option value="">planned + not</option>
          <option value="0">unplanned only</option>
          <option value="1">planned only</option>
        </select>
        <label className="follow" style={{ margin: 0 }}>
          <input type="checkbox" checked={openOnly} onChange={(e) => setOpenOnly(e.target.checked)} /> open only
        </label>
        <button className="btn-sm" onClick={doExport} disabled={!rows?.length}>Export CSV</button>
      </div>
      <p className="subtle" style={{ marginTop: -8, marginBottom: 14, fontSize: 12.5 }}>
        An outage opens after 3 consecutive failed probes and closes on the first success.
        Availability counts unplanned read/write outages only — planned maintenance and
        load-generator gaps are listed but not charged. Times are UTC.
      </p>
      {err && <div className="banner-err">{err} <button className="btn-sm" onClick={load}>Retry</button></div>}

      {rows !== null && perTarget.length > 0 && (
        <div className="card">
          <div className="card-head"><h2>Availability by target
            ({SINCE.find((s) => s.v === since)?.label})</h2></div>
          <table>
            <thead><tr><th>Target</th><th className="num">Uptime</th>
              <th className="num">DB downtime</th><th className="num">Longest</th>
              <th className="num">Unplanned</th><th className="num">Planned</th>
              <th className="num">Load gaps</th><th className="num">Open now</th></tr></thead>
            <tbody>
              {perTarget.map((t) => (
                <tr key={t.jid}>
                  <td><Link to={`/continuous/${t.jid}`}>{jobName(t.jid)}</Link></td>
                  <td className="num mono">
                    {t.uptimePct === null ? "—" : (
                      <b style={t.uptimePct < 99.9 ? { color: "var(--red, #c04747)" } : undefined}>
                        {t.uptimePct.toFixed(t.uptimePct >= 99.99 ? 3 : 2)}%
                      </b>)}
                  </td>
                  <td className="num mono">{fmtDur(t.downS)}</td>
                  <td className="num mono">{t.unplanned ? fmtDur(t.longest) : "—"}</td>
                  <td className="num">{t.unplanned || ""}</td>
                  <td className="num">{t.planned || ""}</td>
                  <td className="num">{t.load || ""}</td>
                  <td className="num">{t.open ? <span className="badge failed">{t.open}</span> : ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {!windowS && <p className="subtle" style={{ fontSize: 12, margin: "8px 0 0" }}>
            Uptime percentages need a bounded window — pick 24h/7d/30d above.</p>}
        </div>
      )}

      <div className="card">
        <div className="card-head"><h2>Ledger</h2>
          <span className="subtle" style={{ fontSize: 12 }}>
            {rows ? `${rows.length} row(s)` : ""}</span></div>
        <table>
          <thead><tr><th>Target</th><th>Kind</th><th>Started (UTC)</th>
            <th>Ended</th><th className="num">Duration</th><th>Class</th><th>First error</th></tr></thead>
          <tbody>
            {rows === null ? (
              <tr><td colSpan={7} className="empty mono">loading…</td></tr>
            ) : rows.length === 0 ? (
              <tr><td colSpan={7} className="empty">
                No outages recorded in this window — either healthy targets or no{" "}
                <Link to="/continuous">continuous workload</Link> probing them.
              </td></tr>
            ) : rows.map((o) => (
              <tr key={o.id}>
                <td><Link to={`/continuous/${o.job_id}`}>{jobName(o.job_id)}</Link></td>
                <td>
                  <span className={`badge ${o.ended_utc ? (o.planned ? "complete" : "canceled") : "failed"}`}>
                    {o.kind}{o.planned ? " · planned" : ""}{!o.ended_utc ? " · open" : ""}
                  </span>
                </td>
                <td className="mono subtle" style={{ fontSize: 12 }} title={o.started_utc}>
                  {fmtWhen(o.started_utc)} <span className="subtle">({relAge(o.started_utc)})</span></td>
                <td className="mono subtle" style={{ fontSize: 12 }}>
                  {o.ended_utc ? fmtWhen(o.ended_utc) : <b>ongoing</b>}</td>
                <td className="num mono">{fmtDur(o.duration_s)}</td>
                <td className="mono" style={{ fontSize: 12 }}>{o.error_class ?? ""}</td>
                <td className="subtle" style={{ maxWidth: 320, fontSize: 12 }}>{o.first_error ?? ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
