import { useEffect, useState } from "react";
import { api } from "../api";

// Environment health (harness `doctor`): version, git SHA, sysbench/psql on PATH.
export function Diagnostics() {
  const [text, setText] = useState("loading…");
  const [ok, setOk] = useState<boolean | null>(null);
  const [busy, setBusy] = useState(false);

  function load() {
    setBusy(true);
    api.get<{ text: string; ok: boolean }>("/api/doctor")
      .then((d) => { setText(d.text); setOk(d.ok); })
      .catch((e) => { setText((e as Error).message); setOk(false); })
      .finally(() => setBusy(false));
  }
  useEffect(load, []);

  return (
    <>
      <div className="toolbar">
        <h1>Diagnostics</h1>
        <div className="spacer" />
        {ok !== null && <span className={`badge ${ok ? "complete" : "failed"}`}>{ok ? "healthy" : "issues"}</span>}
        <button onClick={load} disabled={busy}>{busy ? "Checking…" : "Refresh"}</button>
      </div>
      <p className="subtle" style={{ marginTop: -8, marginBottom: 16 }}>
        Load-generator environment on this server — harness version and git SHA, and whether sysbench
        (with the PostgreSQL driver) and psql are available. This does not connect to any database.
      </p>
      <div className="card"><div className="logpane">{text.split("\n").map((l, i) => <div key={i} className="lg">{l || " "}</div>)}</div></div>
    </>
  );
}
