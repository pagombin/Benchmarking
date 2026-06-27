import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import type { Job, Me, Run } from "../types";
import { fmtInt, fmtWhen, relAge } from "../lib/format";

const STATUSES = ["", "complete", "partial", "running", "failed"];

export function History({ me }: { me: Me }) {
  const [runs, setRuns] = useState<Run[] | null>(null);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [q, setQ] = useState("");
  const [status, setStatus] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const canRun = me.role === "operator" || me.role === "admin";

  async function load() {
    try {
      const [r, j] = await Promise.all([
        api.get<Run[]>("/api/runs"),
        api.get<Job[]>("/api/jobs?active=1"),
      ]);
      setRuns(r);
      setJobs(j);
      setErr(null);            // clear a stale error after a successful reload
    } catch (e) {
      setErr((e as Error).message);
    }
  }

  useEffect(() => {
    load();
  }, []);

  // Keep the active-jobs panel live while anything is in flight.
  useEffect(() => {
    if (jobs.length === 0) return;
    const t = setInterval(load, 4000);
    return () => clearInterval(t);
  }, [jobs.length]);

  const filtered = useMemo(() => {
    if (!runs) return [];
    const needle = q.trim().toLowerCase();
    return runs.filter((r) => {
      if (status && r.status !== status) return false;
      if (!needle) return true;
      return [r.label, r.tags, r.ticket, r.owner, r.run_id, r.target_host]
        .filter(Boolean)
        .some((s) => String(s).toLowerCase().includes(needle));
    });
  }, [runs, q, status]);

  const kpis = useMemo(() => {
    const list = runs ?? [];
    return {
      total: list.length,
      running: jobs.filter((j) => j.state === "running").length,
      queued: jobs.filter((j) => j.state === "queued").length,
      peak: list.reduce((m, r) => Math.max(m, r.peak_qps ?? 0), 0),
    };
  }, [runs, jobs]);

  async function cancel(id: number) {
    if (!confirm(`Cancel job ${id}?`)) return;
    try {
      await api.post(`/api/jobs/${id}/cancel`);
      load();
    } catch (e) {
      alert((e as Error).message);
    }
  }

  async function rerun(id: string) {
    if (!confirm(`Re-run ${id} with the same config?`)) return;
    try {
      const d = await api.post<{ needs_password: boolean }>(`/api/runs/${id}/rerun`);
      if (d.needs_password)
        alert("Re-run queued — but this run had no saved target, so it will fail without a password. "
          + "Use Clone to re-enter credentials, or save the cluster under Targets.");
      load();
    } catch (e) {
      alert((e as Error).message);
    }
  }

  function host(h?: string) {
    if (!h) return "—";
    return h.length > 30 ? h.slice(0, 14) + "…" + h.slice(-12) : h;
  }

  return (
    <>
      <div className="toolbar">
        <h1>Runs</h1>
        <div className="spacer" />
        {canRun && <Link className="btn primary" to="/new">＋ New run</Link>}
      </div>

      {err && <div className="banner-err">{err}</div>}

      <div className="kpi-row" style={{ marginBottom: 18 }}>
        <div className="kpi"><div className="label">Total runs</div><div className="value">{fmtInt(kpis.total)}</div></div>
        <div className="kpi"><div className="label">Running</div><div className="value">{fmtInt(kpis.running)}</div></div>
        <div className="kpi"><div className="label">Queued</div><div className="value">{fmtInt(kpis.queued)}</div></div>
        <div className="kpi"><div className="label">Best peak QPS</div><div className="value">{kpis.peak ? fmtInt(kpis.peak) : "—"}</div></div>
      </div>

      {jobs.length > 0 && (
        <div className="card">
          <div className="card-head"><h2>Active &amp; queued</h2></div>
          <table>
            <thead>
              <tr><th>Job</th><th>Kind</th><th>State</th><th>Run</th><th>By</th><th>Scheduled</th><th></th></tr>
            </thead>
            <tbody>
              {jobs.map((j) => (
                <tr key={j.id}>
                  <td className="mono">{j.id}</td>
                  <td>{j.kind}</td>
                  <td><span className={`badge ${j.state}`}>{j.state}</span></td>
                  <td>{j.run_id ? <Link className="mono" to={`/runs/${j.run_id}`}>{j.run_id}</Link> : "—"}</td>
                  <td className="mono">{j.requested_by}</td>
                  <td className="mono subtle">{j.scheduled_utc ? fmtWhen(j.scheduled_utc) : "now"}</td>
                  <td style={{ textAlign: "right" }}>
                    {canRun && ["queued", "running", "canceling"].includes(j.state) && (
                      <button className="ghost" onClick={() => cancel(j.id)}>Cancel</button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div className="filters">
        <input
          placeholder="Search label, tag, ticket, owner, host…"
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
        <select value={status} onChange={(e) => setStatus(e.target.value)} style={{ width: 180 }}>
          {STATUSES.map((s) => (
            <option key={s} value={s}>{s || "any status"}</option>
          ))}
        </select>
      </div>

      <div className="card">
        <table>
          <thead>
            <tr>
              <th>Run</th><th>Label</th><th>Host</th><th>Mode</th><th>Workload</th>
              <th>Status</th><th className="num">Peak QPS</th><th>Created</th><th></th>
            </tr>
          </thead>
          <tbody>
            {runs === null ? (
              <tr><td colSpan={9} className="empty mono">loading…</td></tr>
            ) : filtered.length === 0 ? (
              <tr><td colSpan={9} className="empty">No runs match. Start one from “New run”.</td></tr>
            ) : (
              filtered.map((r) => (
                <tr key={r.run_id}>
                  <td><Link className="mono" to={`/runs/${r.run_id}`}>{r.run_id}</Link></td>
                  <td>{r.label || "—"}</td>
                  <td className="mono" style={{ fontSize: 12 }} title={r.target_host}>{host(r.target_host)}</td>
                  <td>{r.mode}</td>
                  <td className="mono">{r.workload_type || "—"}</td>
                  <td><span className={`badge ${r.status}`}>{r.status || "—"}</span></td>
                  <td className="num">{r.peak_qps ? fmtInt(r.peak_qps) : "—"}</td>
                  <td className="mono subtle" title={r.created_utc}>{relAge(r.created_utc) || fmtWhen(r.created_utc)}</td>
                  <td className="row-actions">
                    <Link className="btn-sm" to={`/runs/${r.run_id}/report`}>Report</Link>
                    <a className="btn-sm" href={`/runs/${r.run_id}/spec`} target="_blank" rel="noreferrer">Spec</a>
                    {canRun && <Link className="btn-sm" to={`/new?from=${r.run_id}`}>Clone</Link>}
                    {canRun && <button className="btn-sm" onClick={() => rerun(r.run_id)}>Re-run</button>}
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </>
  );
}
