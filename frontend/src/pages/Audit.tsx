import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { fmtWhen } from "../lib/format";

interface AuditRow { id: number; ts_utc: string; username: string | null; action: string; target: string; detail: string; }

export function Audit() {
  const [rows, setRows] = useState<AuditRow[] | null>(null);
  const [q, setQ] = useState("");
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    api.get<AuditRow[]>("/api/audit?limit=1000").then(setRows).catch((e) => setErr(e.message));
  }, []);

  const filtered = useMemo(() => {
    if (!rows) return [];
    const n = q.trim().toLowerCase();
    if (!n) return rows;
    return rows.filter((r) => [r.username, r.action, r.target, r.detail]
      .filter(Boolean).some((s) => String(s).toLowerCase().includes(n)));
  }, [rows, q]);

  return (
    <>
      <div className="toolbar"><h1>Audit log</h1><div className="spacer" />
        <a className="btn" href="/audit/export.csv">Export CSV</a></div>
      {err && <div className="banner-err">{err}</div>}
      <div className="filters"><input placeholder="Search user / action / target / detail…" value={q} onChange={(e) => setQ(e.target.value)} /></div>
      <div className="card">
        <table>
          <thead><tr><th>Time (UTC)</th><th>User</th><th>Action</th><th>Target</th><th>Detail</th></tr></thead>
          <tbody>
            {rows === null ? <tr><td colSpan={5} className="empty mono">loading…</td></tr>
              : filtered.length === 0 ? <tr><td colSpan={5} className="empty">No matching entries.</td></tr>
                : filtered.map((r) => (
                  <tr key={r.id}>
                    <td className="mono subtle" title={r.ts_utc}>{fmtWhen(r.ts_utc)}</td>
                    <td className="mono">{r.username ?? "—"}</td>
                    <td>{r.action}</td>
                    <td className="mono" style={{ fontSize: 12 }}>{r.target}</td>
                    <td className="subtle">{r.detail}</td>
                  </tr>
                ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
