import { useEffect, useState } from "react";
import { api } from "../api";
import type { Me, Target } from "../types";

const BLANK = { name: "", host: "", port: 5432, dbname: "defaultdb", dbuser: "doadmin", sslmode: "require", password: "" };

export function Targets({ me }: { me: Me }) {
  const [targets, setTargets] = useState<Target[] | null>(null);
  const [form, setForm] = useState({ ...BLANK });
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const canEdit = me.role === "operator" || me.role === "admin";

  function load() {
    api.get<Target[]>("/api/targets").then(setTargets).catch((e) => setErr(e.message));
  }
  useEffect(load, []);

  async function create(e: React.FormEvent) {
    e.preventDefault();
    setErr(null);
    setBusy(true);
    try {
      await api.post("/api/targets", form);
      setForm({ ...BLANK });
      load();
    } catch (ex) {
      setErr((ex as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function remove(t: Target) {
    if (!confirm(`Delete target “${t.name}”? Its saved password is erased too.`)) return;
    try {
      await api.del(`/api/targets/${t.id}`);
      load();
    } catch (ex) {
      alert((ex as Error).message);
    }
  }

  const set = (k: keyof typeof form) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) =>
    setForm({ ...form, [k]: k === "port" ? Number(e.target.value) : e.target.value });

  return (
    <>
      <div className="toolbar"><h1>Targets</h1></div>
      <p className="subtle" style={{ marginTop: -8, marginBottom: 16 }}>
        Saved clusters. The password is stored encrypted (Fernet, off-database) and injected only at run
        time — it never appears in a spec, the database, logs, reports, or artifacts. Saving a target lets
        you start and re-run against it without re-entering credentials.
      </p>

      {err && <div className="banner-err">{err}</div>}

      <div className="grid2">
        <div className="card">
          <div className="card-head"><h2>Saved clusters</h2></div>
          <table>
            <thead><tr><th>Name</th><th>Host</th><th className="num">Port</th><th>Database</th><th>User</th><th>SSL</th><th></th></tr></thead>
            <tbody>
              {targets === null ? (
                <tr><td colSpan={7} className="empty mono">loading…</td></tr>
              ) : targets.length === 0 ? (
                <tr><td colSpan={7} className="empty">No saved targets yet.</td></tr>
              ) : targets.map((t) => (
                <tr key={t.id}>
                  <td>{t.name}</td>
                  <td className="mono" style={{ fontSize: 12 }}>{t.host}</td>
                  <td className="num">{t.port}</td>
                  <td className="mono">{t.dbname}</td>
                  <td className="mono">{t.dbuser}</td>
                  <td>{t.sslmode}</td>
                  <td style={{ textAlign: "right" }}>
                    {canEdit && <button className="ghost" onClick={() => remove(t)}>Delete</button>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        {canEdit && (
          <div className="card">
            <div className="card-head"><h2>Add a cluster</h2></div>
            <form onSubmit={create}>
              <div className="field"><label>Name</label><input value={form.name} onChange={set("name")} placeholder="advanced-nyc3-prod" required /></div>
              <div className="field"><label>Host</label><input value={form.host} onChange={set("host")} placeholder="private-db.nyc3.db.ondigitalocean.com" required /></div>
              <div className="row">
                <div className="field"><label>Port</label><input type="number" value={form.port} onChange={set("port")} /></div>
                <div className="field"><label>SSL mode</label>
                  <select value={form.sslmode} onChange={set("sslmode")}>
                    {["require", "verify-full", "verify-ca", "prefer", "disable"].map((s) => <option key={s}>{s}</option>)}
                  </select>
                </div>
              </div>
              <div className="row">
                <div className="field"><label>Database</label><input value={form.dbname} onChange={set("dbname")} /></div>
                <div className="field"><label>User</label><input value={form.dbuser} onChange={set("dbuser")} /></div>
              </div>
              <div className="field"><label>Password (stored encrypted)</label>
                <input type="password" value={form.password} onChange={set("password")} autoComplete="off" /></div>
              <button className="primary" disabled={busy} type="submit">{busy ? "Saving…" : "Save cluster"}</button>
            </form>
          </div>
        )}
      </div>
    </>
  );
}
