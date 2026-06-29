import { useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../api";
import { openJobStream, type CheckEvent } from "../lib/sse";
import type { Job, PrepareStats } from "../types";
import { durBetween, fmtInt, fmtNum, fmtWhen } from "../lib/format";

const ICON: Record<string, string> = { ok: "✓", warn: "!", fail: "✕", info: "·" };

export function JobView() {
  const { jobId = "" } = useParams();
  const [checks, setChecks] = useState<CheckEvent[]>([]);
  const [log, setLog] = useState("");
  const [status, setStatus] = useState("running");
  const [job, setJob] = useState<(Job & { prepare_stats?: PrepareStats | null }) | null>(null);
  const pane = useRef<HTMLDivElement>(null);

  function loadJob() {
    api.get<Job & { prepare_stats?: PrepareStats | null }>(`/api/jobs/${jobId}`).then(setJob).catch(() => {});
  }

  useEffect(() => {
    loadJob();
    const es = openJobStream(Number(jobId), {
      onCheck: (c) => setChecks((p) => [...p, c]),
      onLog: (l) => setLog((p) => p + l),
      onDone: (d) => { setStatus(d.status); loadJob(); },   // refresh for prepare metrics + final state
      onError: () => {},
    });
    return () => es.close();
  }, [jobId]);

  useEffect(() => {
    if (pane.current) pane.current.scrollTop = pane.current.scrollHeight;
  }, [log]);

  // The harness emits a trailing summary row named "Preflight"; treat it as overall.
  const rows = checks.filter((c) => c.name !== "Preflight");
  const summary = checks.find((c) => c.name === "Preflight");
  const ps = job?.prepare_stats;
  const jobState = job?.state ?? status;

  return (
    <>
      <div className="toolbar">
        <div><h1>{job?.kind ? `${job.kind} task` : "Task"}</h1>
          <div className="subtle mono" style={{ fontSize: 12 }}>job {jobId}</div></div>
        <div className="spacer" />
        <span className={`badge ${jobState === "done" ? "complete" : jobState}`}>{jobState}</span>
        <Link className="btn" to="/tasks">← Tasks</Link>
      </div>

      {job?.error && <div className="banner-err">{job.error}</div>}

      {job?.kind === "prepare" && (
        ps ? (
          <>
            <div className="kpi-row" style={{ marginBottom: 16 }}>
              <div className="kpi"><div className="label">Loaded</div><div className="value" style={{ fontSize: 18 }}>{ps.loaded_units ?? "—"}</div></div>
              <div className="kpi"><div className="label">Wall time</div><div className="value">{ps.wall_s != null ? durBetween(ps.started_utc, ps.finished_utc) : "—"}</div></div>
              <div className="kpi"><div className="label">Data size</div><div className="value" style={{ fontSize: 20 }}>{ps.db_size_pretty ?? "—"}</div></div>
              <div className="kpi"><div className="label">Throughput</div><div className="value">{ps.load_mb_s != null ? fmtNum(ps.load_mb_s) : "—"}<small> MB/s</small></div></div>
            </div>
            <div className="card"><div className="card-head"><h2>Prepare details</h2></div>
              <table><tbody>
                <tr><td style={{ width: 180, color: "var(--muted)" }}>Started</td><td className="mono">{fmtWhen(ps.started_utc)}</td></tr>
                <tr><td style={{ color: "var(--muted)" }}>Finished</td><td className="mono">{fmtWhen(ps.finished_utc)}</td></tr>
                <tr><td style={{ color: "var(--muted)" }}>Wall seconds</td><td className="mono num">{ps.wall_s != null ? fmtInt(ps.wall_s) : "—"}</td></tr>
                <tr><td style={{ color: "var(--muted)" }}>Load threads</td><td className="mono">{ps.load_threads ?? "—"}</td></tr>
                {ps.target_host && <tr><td style={{ color: "var(--muted)" }}>Target</td><td className="mono">{ps.target_host}/{ps.database}</td></tr>}
              </tbody></table>
            </div>
          </>
        ) : jobState === "done" ? (
          <div className="card"><p className="subtle">Prepare finished — no load metrics recorded (the dataset may have already been present, or the load did not complete). See the console below.</p></div>
        ) : null
      )}

      {rows.length > 0 && (
        <div className="card">
          <div className="card-head"><h2>Checks</h2>{summary && <span className={`chip status-${summary.status}`}>{summary.detail}</span>}</div>
          <div className="checklist">
            {rows.map((c, i) => (
              <div key={i} className={`check status-${c.status}`}>
                <span className="ci">{ICON[c.status] ?? "·"}</span>
                <span className="cn">{c.name}</span>
                <span className="cd mono">{c.detail}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      <div className="card">
        <div className="card-head"><h2>Console</h2></div>
        <div className="logpane" ref={pane}>
          {log === "" ? <div className="subtle mono">waiting for output…</div>
            : log.split("\n").map((l, i) => <div key={i} className="lg">{l || " "}</div>)}
        </div>
      </div>
    </>
  );
}
