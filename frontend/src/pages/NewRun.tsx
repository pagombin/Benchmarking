import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { api } from "../api";
import type { Me, Target } from "../types";

// Minimal YAML emitter for flat/nested scalar+array specs (no dep; CSP-safe).
function toYaml(o: Record<string, unknown>, indent = ""): string {
  const lines: string[] = [];
  for (const [k, v] of Object.entries(o)) {
    if (v === undefined || v === null || v === "") continue;
    if (Array.isArray(v)) lines.push(`${indent}${k}: [${v.join(", ")}]`);
    else if (typeof v === "object") {
      const inner = toYaml(v as Record<string, unknown>, indent + "  ");
      if (inner) { lines.push(`${indent}${k}:`); lines.push(inner); }
    } else lines.push(`${indent}${k}: ${v}`);
  }
  return lines.join("\n");
}

const WORKLOADS = ["tpcc", "oltp_read_write", "oltp_read_only", "oltp_write_only"];

export function NewRun({ me }: { me: Me }) {
  const canRun = me.role === "operator" || me.role === "admin";
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const cloneFrom = params.get("from") || "";

  const [targets, setTargets] = useState<Target[]>([]);
  const [targetMode, setTargetMode] = useState<"saved" | "inline">("inline");
  const [targetId, setTargetId] = useState<number>(0);
  const [inline, setInline] = useState({ host: "", port: 5432, database: "defaultdb", user: "doadmin", sslmode: "require", password: "" });
  const [meta, setMeta] = useState({ label: "", edition: "advanced", tshirt_size: "8c32g", tags: "", ticket: "" });
  const [wl, setWl] = useState({ type: "tpcc", tpcc_path: "/opt/sysbench-tpcc", tables: 10, scale: 30, table_size: 1000000 });
  const [mode, setMode] = useState<"sweep" | "soak">("sweep");
  const [sweep, setSweep] = useState({ threads: "1, 4, 16, 64", duration_s: 300, warmup_s: 60, cooldown_s: 30, repetitions: 1 });
  const [soak, setSoak] = useState({ threads: 64, duration_s: 3600, tolerate_errors: true });
  const [schedule, setSchedule] = useState("");
  const [tplName, setTplName] = useState("");

  const [yaml, setYaml] = useState("");
  const [autoSync, setAutoSync] = useState(true);
  const [validateOut, setValidateOut] = useState<{ ok: boolean; msg: string } | null>(null);
  const [dryOut, setDryOut] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const firstBuild = useRef(true);

  useEffect(() => {
    api.get<Target[]>("/api/targets").then((t) => {
      setTargets(t);
      if (t.length) { setTargetMode("saved"); setTargetId(t[0].id); }
    }).catch(() => {});
  }, []);

  // Clone: load a prior run's spec into the editor (raw becomes authoritative).
  useEffect(() => {
    if (!cloneFrom) return;
    api.raw(`/runs/${cloneFrom}/spec`).then((r) => r.text()).then((txt) => {
      setYaml(txt); setAutoSync(false);
    }).catch(() => {});
  }, [cloneFrom]);

  const doc = useMemo(() => {
    const tgt = targetMode === "saved" ? targets.find((t) => t.id === targetId) : null;
    const target = tgt
      ? { host: tgt.host, port: tgt.port, database: tgt.dbname, user: tgt.dbuser, sslmode: tgt.sslmode, password_env: "PGB_TARGET_PASSWORD" }
      : { host: inline.host, port: inline.port, database: inline.database, user: inline.user, sslmode: inline.sslmode, password_env: "PGB_TARGET_PASSWORD" };
    const run: Record<string, unknown> = { label: meta.label || "run", edition: meta.edition, tshirt_size: meta.tshirt_size };
    const tags = meta.tags.split(",").map((s) => s.trim()).filter(Boolean);
    if (tags.length) run.tags = tags;
    if (meta.ticket) run.ticket = meta.ticket;
    const workload = wl.type === "tpcc"
      ? { type: wl.type, tpcc_path: wl.tpcc_path, tables: wl.tables, scale: wl.scale }
      : { type: wl.type, tables: 1, table_size: wl.table_size };
    const d: Record<string, unknown> = { run, target, workload };
    if (mode === "soak") d.soak = { threads: soak.threads, duration_s: soak.duration_s, tolerate_errors: soak.tolerate_errors };
    else d.sweep = {
      threads: sweep.threads.split(",").map((s) => parseInt(s.trim(), 10)).filter((n) => !Number.isNaN(n)),
      duration_s: sweep.duration_s, warmup_s: sweep.warmup_s, cooldown_s: sweep.cooldown_s, repetitions: sweep.repetitions,
    };
    return d;
  }, [targetMode, targetId, targets, inline, meta, wl, mode, sweep, soak]);

  useEffect(() => {
    if (autoSync) { setYaml(toYaml(doc) + "\n"); }
    firstBuild.current = false;
  }, [doc, autoSync]);

  async function validate() {
    setErr(null);
    try {
      const d = await api.post<{ ok: boolean; mode: string; label: string; workload: string; error?: string; hint?: string }>(
        "/api/validate", { spec_yaml: yaml });
      setValidateOut(d.ok
        ? { ok: true, msg: `valid — ${d.mode} run “${d.label}” (${d.workload})` }
        : { ok: false, msg: (d.error || "invalid") + (d.hint ? ` — ${d.hint}` : "") });
    } catch (e) { setValidateOut({ ok: false, msg: (e as Error).message }); }
  }
  async function dryRun() {
    setErr(null);
    try {
      const d = await api.post<{ mode: string; budget_s: number; commands: string[] }>("/api/dry-run", { spec_yaml: yaml });
      setDryOut(`# ${d.mode} — planned wall-clock ~${Math.round(d.budget_s / 60)} min (${d.budget_s}s)\n` + d.commands.join("\n"));
    } catch (e) { setDryOut("error: " + (e as Error).message); }
  }
  async function start() {
    setErr(null);
    try {
      const body: Record<string, unknown> = { spec_yaml: yaml, scheduled_utc: schedule.trim() || null };
      if (targetMode === "saved") body.target_id = targetId;
      else body.password = inline.password;
      await api.post("/api/runs", body);
      window.location.href = "/ui";
    } catch (e) { setErr("could not start: " + (e as Error).message); }
  }
  async function task(kind: "preflight" | "prepare") {
    setErr(null);
    try {
      const body: Record<string, unknown> = { spec_yaml: yaml };
      if (targetMode === "saved") body.target_id = targetId;
      else body.password = inline.password;
      const d = await api.post<{ job_id: number }>(`/api/${kind}`, body);
      navigate(`/jobs/${d.job_id}`);
    } catch (e) { setErr(`could not start ${kind}: ` + (e as Error).message); }
  }
  async function saveTemplate() {
    try {
      const d = await api.post<{ name: string; version: number }>("/api/templates", { name: tplName, spec_yaml: yaml });
      setErr(null); setDryOut(`saved template “${d.name}” v${d.version}`);
    } catch (e) { setErr((e as Error).message); }
  }

  const seti = (k: keyof typeof inline) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) =>
    setInline({ ...inline, [k]: k === "port" ? Number(e.target.value) : e.target.value });

  return (
    <>
      <div className="toolbar"><h1>New run</h1>{cloneFrom && <span className="chip">cloned from <span className="mono">{cloneFrom}</span></span>}</div>
      {err && <div className="banner-err">{err}</div>}

      <div className="grid2">
        <div className="card">
          <div className="card-head"><h2>Configure</h2><div className="spacer" />
            {!autoSync && <button className="ghost" onClick={() => setAutoSync(true)}>↻ Rebuild from fields</button>}
          </div>

          <label>Target cluster</label>
          <div className="seg">
            <button className={targetMode === "saved" ? "on" : ""} onClick={() => setTargetMode("saved")} disabled={!targets.length}>Saved target</button>
            <button className={targetMode === "inline" ? "on" : ""} onClick={() => setTargetMode("inline")}>Enter host</button>
          </div>
          {targetMode === "saved" ? (
            <div className="field">
              <select value={targetId} onChange={(e) => setTargetId(Number(e.target.value))}>
                {targets.map((t) => <option key={t.id} value={t.id}>{t.name} — {t.host}</option>)}
              </select>
              <p className="subtle" style={{ fontSize: 12, marginTop: 4 }}>Uses the cluster's saved encrypted password.</p>
            </div>
          ) : (
            <>
              <div className="field"><label>Host</label><input value={inline.host} onChange={seti("host")} placeholder="private-db.nyc3.db.ondigitalocean.com" /></div>
              <div className="row">
                <div className="field"><label>Port</label><input type="number" value={inline.port} onChange={seti("port")} /></div>
                <div className="field"><label>SSL</label>
                  <select value={inline.sslmode} onChange={seti("sslmode")}>{["require", "verify-full", "verify-ca", "prefer", "disable"].map((s) => <option key={s}>{s}</option>)}</select></div>
              </div>
              <div className="row">
                <div className="field"><label>Database</label><input value={inline.database} onChange={seti("database")} /></div>
                <div className="field"><label>User</label><input value={inline.user} onChange={seti("user")} /></div>
              </div>
              <div className="field"><label>Password (stored encrypted, never in spec/DB/report)</label>
                <input type="password" value={inline.password} onChange={seti("password")} autoComplete="off" /></div>
            </>
          )}

          <hr />
          <div className="row">
            <div className="field"><label>Label</label><input value={meta.label} onChange={(e) => setMeta({ ...meta, label: e.target.value })} placeholder="advanced-8c32g-tpcc" /></div>
            <div className="field"><label>Edition</label><select value={meta.edition} onChange={(e) => setMeta({ ...meta, edition: e.target.value })}><option>advanced</option><option>standard</option></select></div>
          </div>
          <div className="row">
            <div className="field"><label>T-shirt size</label><input value={meta.tshirt_size} onChange={(e) => setMeta({ ...meta, tshirt_size: e.target.value })} /></div>
            <div className="field"><label>Ticket</label><input value={meta.ticket} onChange={(e) => setMeta({ ...meta, ticket: e.target.value })} placeholder="DBAAS-1234" /></div>
          </div>
          <div className="field"><label>Tags (comma)</label><input value={meta.tags} onChange={(e) => setMeta({ ...meta, tags: e.target.value })} placeholder="nightly, scaling" /></div>

          <hr />
          <div className="row">
            <div className="field"><label>Workload</label><select value={wl.type} onChange={(e) => setWl({ ...wl, type: e.target.value })}>{WORKLOADS.map((w) => <option key={w}>{w}</option>)}</select></div>
            <div className="field"><label>Mode</label><select value={mode} onChange={(e) => setMode(e.target.value as "sweep" | "soak")}><option value="sweep">sweep</option><option value="soak">soak</option></select></div>
          </div>
          {wl.type === "tpcc" ? (
            <div className="row">
              <div className="field"><label>TPCC path</label><input value={wl.tpcc_path} onChange={(e) => setWl({ ...wl, tpcc_path: e.target.value })} /></div>
              <div className="field"><label>Tables</label><input type="number" value={wl.tables} onChange={(e) => setWl({ ...wl, tables: Number(e.target.value) })} /></div>
              <div className="field"><label>Scale (warehouses)</label><input type="number" value={wl.scale} onChange={(e) => setWl({ ...wl, scale: Number(e.target.value) })} /></div>
            </div>
          ) : (
            <div className="field"><label>Table size (rows)</label><input type="number" value={wl.table_size} onChange={(e) => setWl({ ...wl, table_size: Number(e.target.value) })} /></div>
          )}
          {mode === "sweep" ? (
            <>
              <div className="field"><label>Thread ladder (comma)</label><input value={sweep.threads} onChange={(e) => setSweep({ ...sweep, threads: e.target.value })} /></div>
              <div className="row">
                <div className="field"><label>Duration (s)</label><input type="number" value={sweep.duration_s} onChange={(e) => setSweep({ ...sweep, duration_s: Number(e.target.value) })} /></div>
                <div className="field"><label>Warmup (s)</label><input type="number" value={sweep.warmup_s} onChange={(e) => setSweep({ ...sweep, warmup_s: Number(e.target.value) })} /></div>
                <div className="field"><label>Reps</label><input type="number" value={sweep.repetitions} onChange={(e) => setSweep({ ...sweep, repetitions: Number(e.target.value) })} /></div>
              </div>
            </>
          ) : (
            <div className="row">
              <div className="field"><label>Threads</label><input type="number" value={soak.threads} onChange={(e) => setSoak({ ...soak, threads: Number(e.target.value) })} /></div>
              <div className="field"><label>Duration (s)</label><input type="number" value={soak.duration_s} onChange={(e) => setSoak({ ...soak, duration_s: Number(e.target.value) })} /></div>
            </div>
          )}

          <hr />
          <div className="field"><label>Start at (UTC, optional — blank = now)</label><input value={schedule} onChange={(e) => setSchedule(e.target.value)} placeholder="2026-06-28T02:00:00Z" /></div>
          <div className="row">
            <div className="field"><label>Save current spec as template</label><input value={tplName} onChange={(e) => setTplName(e.target.value)} placeholder="template name" /></div>
            <button className="ghost" style={{ alignSelf: "end", marginBottom: 12 }} onClick={saveTemplate} disabled={!tplName}>Save template</button>
          </div>
        </div>

        <div className="card">
          <div className="card-head"><h2>Spec (YAML)</h2>{!autoSync && <span className="chip">raw editing</span>}</div>
          <textarea className="yaml" value={yaml} spellCheck={false} rows={20}
            onChange={(e) => { setYaml(e.target.value); setAutoSync(false); }} />
          {validateOut && <div className={`out ${validateOut.ok ? "ok" : "bad"}`}>{validateOut.msg}</div>}
          <div className="actions" style={{ marginTop: 10 }}>
            <button onClick={validate}>Validate</button>
            <button onClick={dryRun}>Dry-run</button>
            {canRun && <button onClick={() => task("preflight")}>Preflight</button>}
            {canRun && <button onClick={() => task("prepare")}>Prepare data</button>}
            {canRun ? <button className="primary" onClick={start}>Start run</button>
              : <span className="subtle">viewer role: read-only</span>}
          </div>
          {dryOut && <pre className="out mono dry">{dryOut}</pre>}
        </div>
      </div>
    </>
  );
}
