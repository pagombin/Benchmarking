import { useEffect, useMemo, useRef, useState } from "react";

function severity(line: string): string {
  const l = line.toLowerCase();
  if (/\b(fatal|error|failed|traceback)\b/.test(l)) return "lg-err";
  if (/\b(warn|warning)\b/.test(l)) return "lg-warn";
  return "";
}

export function LogConsole({ text }: { text: string }) {
  const [q, setQ] = useState("");
  const [follow, setFollow] = useState(true);
  const pane = useRef<HTMLDivElement>(null);

  const lines = useMemo(() => {
    const all = text.split("\n");
    if (!q) return all;
    const needle = q.toLowerCase();
    return all.filter((l) => l.toLowerCase().includes(needle));
  }, [text, q]);

  useEffect(() => {
    if (follow && pane.current) pane.current.scrollTop = pane.current.scrollHeight;
  }, [lines, follow]);

  return (
    <div className="card">
      <div className="card-head">
        <h2>Console</h2>
        <div className="spacer" />
        <input
          className="log-search"
          placeholder="filter…"
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
        <label className="follow">
          <input type="checkbox" checked={follow} onChange={(e) => setFollow(e.target.checked)} /> follow
        </label>
      </div>
      <div className="logpane" ref={pane}>
        {text === "" ? (
          <div className="subtle mono">waiting for output…</div>
        ) : (
          lines.map((l, i) => (
            <div key={i} className={`lg ${severity(l)}`}>{l || " "}</div>
          ))
        )}
      </div>
    </div>
  );
}
