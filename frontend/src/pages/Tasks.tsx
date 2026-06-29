import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import type { Job } from "../types";
import { durBetween, relAge } from "../lib/format";

// All jobs — including prepare / preflight / doctor, which produce no run dir and
// so weren't visible on the Runs page. Each links to its live/finished detail.
const KINDS = ["", "run", "soak", "prepare", "preflight", "doctor"];

export function Tasks() {
  const [jobs, setJobs] = useState<Job[] | null>(null);
  const [kind, setKind] = useState("");
  const [err, setErr] = useState<string | null>(null);

  async function load() {
    try { setJobs(await api.get<Job[]>("/api/jobs")); setErr(null); }
    catch (e) { setErr((e as Error).message); }
  }
  useEffect(() => { load(); }, []);
  useEffect(() => {
    const anyActive = (jobs ?? []).some((j) => ["queued", "running", "canceling"].includes(j.state));
    if (!anyActive) return;
    const t = setInterval(load, 4000);
    return () => clearInterval(t);
  }, [jobs]);

  const rows = useMemo(() => (jobs ?? []).filter((j) => !kind || j.kind === kind), [jobs, kind]);

  function link(j: Job) {
    if (j.kind === "run" || j.kind === "soak") {
      return j.run_id ? <Link className="mono" to={`/runs/${j.run_id}`}>{j.run_id}</Link> : <span className="subtle">—</span>;
    }
    return <Link className="mono" to={`/jobs/${j.id}`}>view</Link>;   // prepare/preflight/doctor
  }

  return (
    <>
      <div className="toolbar"><h1>Tasks</h1><div className="spacer" />
        <select value={kind} onChange={(e) => setKind(e.target.value)} style={{ width: 160 }}>
          {KINDS.map((k) => <option key={k} value={k}>{k || "all kinds"}</option>)}
        </select>
      </div>
      <p className="subtle" style={{ marginTop: -8, marginBottom: 14, fontSize: 12.5 }}>
        Every queued/running/finished task — runs, soaks, and the lifecycle jobs (prepare, preflight, doctor).
        Click a task to see its live output, result, and (for prepare) the data-load metrics.
      </p>
      {err && <div className="banner-err">{err}</div>}
      <div className="card">
        <table>
          <thead><tr><th>Job</th><th>Kind</th><th>State</th><th>Detail</th><th>By</th>
            <th>Started</th><th className="num">Duration</th><th>Error</th></tr></thead>
          <tbody>
            {jobs === null ? <tr><td colSpan={8} className="empty mono">loading…</td></tr>
              : rows.length === 0 ? <tr><td colSpan={8} className="empty">No tasks yet.</td></tr>
                : rows.map((j) => (
                  <tr key={j.id}>
                    <td className="mono">{j.id}</td>
                    <td>{j.kind}</td>
                    <td><span className={`badge ${j.state === "done" ? "complete" : j.state}`}>{j.state}</span></td>
                    <td>{link(j)}</td>
                    <td className="mono">{j.requested_by}</td>
                    <td className="mono subtle" title={j.started_utc ?? ""}>{j.started_utc ? relAge(j.started_utc) : "—"}</td>
                    <td className="num">{durBetween(j.started_utc, j.finished_utc)}</td>
                    <td className="subtle" style={{ maxWidth: 280, fontSize: 12 }}>{j.error || ""}</td>
                  </tr>
                ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
