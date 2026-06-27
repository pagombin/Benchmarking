import { useState } from "react";
import { Link, useParams } from "react-router-dom";

// Renders a run's self-contained report inline (same-origin iframe), for past or
// in-flight runs — no download required. Download / open-raw / regenerate remain.
export function ReportView() {
  const { runId = "" } = useParams();
  const [bust, setBust] = useState(0);
  const src = `/runs/${encodeURIComponent(runId)}/report${bust ? `?regen=1&_=${bust}` : ""}`;
  return (
    <>
      <div className="toolbar">
        <div>
          <h1>Report</h1>
          <div className="subtle mono" style={{ fontSize: 12 }}>{runId}</div>
        </div>
        <div className="spacer" />
        <Link className="btn" to={`/runs/${runId}`}>← Cockpit</Link>
        <button onClick={() => setBust((b) => b + 1)}>Regenerate</button>
        <a className="btn" href={`/runs/${runId}/report/download`}>Download</a>
        <a className="btn" href={`/runs/${runId}/report`} target="_blank" rel="noreferrer">Open raw ↗</a>
      </div>
      <div className="report-frame">
        <iframe title={`report ${runId}`} src={src} />
      </div>
    </>
  );
}
