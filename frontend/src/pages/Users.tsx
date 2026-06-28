import { useEffect, useState } from "react";
import { api } from "../api";
import type { Me } from "../types";

interface UserRow { id: number; username: string; role: string; disabled: number; created_utc: string; }
const ROLES = ["viewer", "operator", "admin"];

export function Users({ me }: { me: Me }) {
  const [users, setUsers] = useState<UserRow[] | null>(null);
  const [form, setForm] = useState({ username: "", password: "", role: "viewer" });
  const [err, setErr] = useState<string | null>(null);

  function load() {
    api.get<UserRow[]>("/api/users").then(setUsers).catch((e) => setErr(e.message));
  }
  useEffect(load, []);

  async function create(e: React.FormEvent) {
    e.preventDefault();
    setErr(null);
    try {
      await api.post("/api/users", form);
      setForm({ username: "", password: "", role: "viewer" });
      load();
    } catch (ex) { setErr((ex as Error).message); }
  }
  async function update(username: string, patch: Record<string, unknown>) {
    try { await api.post(`/api/users/${encodeURIComponent(username)}`, patch); load(); }
    catch (ex) { alert((ex as Error).message); }
  }
  async function resetPw(username: string) {
    const pw = prompt(`New password for ${username}:`);
    if (pw) update(username, { password: pw });
  }

  return (
    <>
      <div className="toolbar"><h1>Users</h1></div>
      {err && <div className="banner-err">{err}</div>}
      <div className="grid2">
        <div className="card">
          <div className="card-head"><h2>Accounts</h2></div>
          <table>
            <thead><tr><th>User</th><th>Role</th><th>Status</th><th></th></tr></thead>
            <tbody>
              {users === null ? <tr><td colSpan={4} className="empty mono">loading…</td></tr>
                : users.map((u) => (
                  <tr key={u.id}>
                    <td className="mono">{u.username}{u.username === me.user && <span className="subtle"> (you)</span>}</td>
                    <td>
                      <select value={u.role} disabled={u.username === me.user}
                        onChange={(e) => update(u.username, { role: e.target.value })}>
                        {ROLES.map((r) => <option key={r}>{r}</option>)}
                      </select>
                    </td>
                    <td>{u.disabled ? <span className="badge failed">disabled</span> : <span className="badge ok">active</span>}</td>
                    <td className="row-actions">
                      <button className="btn-sm" onClick={() => resetPw(u.username)}>Reset password</button>
                      {u.username !== me.user && (
                        <button className="btn-sm" onClick={() => update(u.username, { disabled: !u.disabled })}>
                          {u.disabled ? "Enable" : "Disable"}
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
            </tbody>
          </table>
        </div>
        <div className="card">
          <div className="card-head"><h2>Add user</h2></div>
          <form onSubmit={create}>
            <div className="field"><label>Username</label><input value={form.username} onChange={(e) => setForm({ ...form, username: e.target.value })} required /></div>
            <div className="field"><label>Password</label><input type="password" value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} autoComplete="new-password" required /></div>
            <div className="field"><label>Role</label>
              <select value={form.role} onChange={(e) => setForm({ ...form, role: e.target.value })}>{ROLES.map((r) => <option key={r}>{r}</option>)}</select></div>
            <p className="subtle" style={{ fontSize: 12 }}>
              <b>viewer</b>: read-only · <b>operator</b>: start/cancel/manage runs &amp; targets · <b>admin</b>: users + settings.
            </p>
            <button className="primary" type="submit">Create user</button>
          </form>
        </div>
      </div>
    </>
  );
}
