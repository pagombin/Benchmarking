import { useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { openJobStream, type CheckEvent } from "../lib/sse";

const ICON: Record<string, string> = { ok: "✓", warn: "!", fail: "✕", info: "·" };

export function JobView() {
  const { jobId = "" } = useParams();
  const [checks, setChecks] = useState<CheckEvent[]>([]);
  const [log, setLog] = useState("");
  const [status, setStatus] = useState("running");
  const pane = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const es = openJobStream(Number(jobId), {
      onCheck: (c) => setChecks((p) => [...p, c]),
      onLog: (l) => setLog((p) => p + l),
      onDone: (d) => setStatus(d.status),
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

  return (
    <>
      <div className="toolbar">
        <div><h1>Task</h1><div className="subtle mono" style={{ fontSize: 12 }}>job {jobId}</div></div>
        <div className="spacer" />
        <span className={`badge ${status === "done" ? "complete" : status}`}>{status}</span>
        <Link className="btn" to="/">← Runs</Link>
      </div>

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
