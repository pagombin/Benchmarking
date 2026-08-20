import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import type { Job } from "../types";
import { durBetween, relAge } from "../lib/format";
import { usePageTitle } from "../lib/ui";

// All jobs — including prepare / preflight / doctor, which produce no run dir and
// so weren't visible on the Runs page. Each links to its live/finished detail.
// The kind filter is derived from the data so new job kinds (continuous,
// device_probe, ops_*, …) can never silently vanish from the dropdown (B-010).
const KIND_ORDER = ["run", "soak", "continuous", "prepare", "preflight", "doctor"];

export function Tasks() {
  usePageTitle("Tasks");
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

  const kinds = useMemo(() => {
    const seen = Array.from(new Set((jobs ?? []).map((j) => j.kind)));
    seen.sort((a, b) => {
      const ia = KIND_ORDER.indexOf(a), ib = KIND_ORDER.indexOf(b);
      return (ia === -1 ? 99 : ia) - (ib === -1 ? 99 : ib) || a.localeCompare(b);
    });
    return ["", ...seen];
  }, [jobs]);

  function link(j: Job) {
    if (j.kind === "continuous") {
      return <Link className="mono" to={`/continuous/${j.id}`}>view</Link>;
    }
    if (j.kind === "run" || j.kind === "soak") {
      return j.run_id ? <Link className="mono" to={`/runs/${j.run_id}`}>{j.run_id}</Link> : <span className="subtle">—</span>;
    }
    return <Link className="mono" to={`/jobs/${j.id}`}>view</Link>;   // prepare/preflight/doctor/…
  }

  return (
    <>
      <div className="toolbar"><h1>Tasks</h1><div className="spacer" />
        <select value={kind} onChange={(e) => setKind(e.target.value)} style={{ width: 160 }}>
          {kinds.map((k) => <option key={k} value={k}>{k || "all kinds"}</option>)}
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
