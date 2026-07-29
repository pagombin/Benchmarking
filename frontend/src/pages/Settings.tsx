import { useEffect, useState } from "react";
import { api } from "../api";

interface SmtpCfg { host: string; port: number; user: string; from: string; to: string; tls: boolean; }
interface SettingsResp {
  notify: { smtp?: Partial<SmtpCfg>; slack?: { enabled?: boolean } };
  base_url: string;
  do_cluster_id: string;
  max_concurrency: number;
  has_smtp_pw: boolean;
  has_slack: boolean;
  has_do_token: boolean;
}

export function Settings() {
  const [s, setS] = useState<SettingsResp | null>(null);
  const [smtp, setSmtp] = useState<SmtpCfg>({ host: "", port: 587, user: "", from: "", to: "", tls: true });
  const [slackEnabled, setSlackEnabled] = useState(false);
  const [concurrency, setConcurrency] = useState(1);
  const [doCluster, setDoCluster] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [smtpPw, setSmtpPw] = useState("");
  const [slackHook, setSlackHook] = useState("");
  const [doToken, setDoToken] = useState("");
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [testOut, setTestOut] = useState("");

  function load() {
    api.get<SettingsResp>("/api/admin/settings").then((d) => {
      setS(d);
      setSmtp({ host: d.notify.smtp?.host ?? "", port: d.notify.smtp?.port ?? 587, user: d.notify.smtp?.user ?? "",
        from: d.notify.smtp?.from ?? "", to: d.notify.smtp?.to ?? "", tls: d.notify.smtp?.tls ?? true });
      setSlackEnabled(!!d.notify.slack?.enabled);
      setConcurrency(d.max_concurrency);
      setDoCluster(d.do_cluster_id);
      setBaseUrl(d.base_url);
    }).catch((e) => setErr(e.message));
  }
  useEffect(load, []);

  async function save() {
    setErr(null); setMsg(null);
    try {
      await api.post("/api/admin/settings", {
        smtp, slack: { enabled: slackEnabled }, max_concurrency: concurrency,
        do_cluster_id: doCluster, base_url: baseUrl,
        ...(smtpPw ? { smtp_password: smtpPw } : {}),
        ...(slackHook ? { slack_webhook: slackHook } : {}),
        ...(doToken ? { do_api_token: doToken } : {}),
      });
      setSmtpPw(""); setSlackHook(""); setDoToken("");
      setMsg("Saved.");
      load();
    } catch (e) { setErr((e as Error).message); }
  }
  async function sendTest() {
    setTestOut("sending…");
    try {
      const d = await api.post<{ sent: string[] }>("/api/notify/test");
      setTestOut(d.sent.length ? "sent via: " + d.sent.join(", ") : "nothing configured (no channels)");
    } catch (e) { setTestOut((e as Error).message); }
  }

  if (!s) return <div className="subtle mono" style={{ padding: 20 }}>{err ?? "loading…"}</div>;
  const setField = (k: keyof SmtpCfg) => (e: React.ChangeEvent<HTMLInputElement>) =>
    setSmtp({ ...smtp, [k]: k === "port" ? Number(e.target.value) : e.target.value });

  return (
    <>
      <div className="toolbar"><h1>Settings</h1><div className="spacer" />
        {msg && <span className="out ok">{msg}</span>}
        <button className="primary" onClick={save}>Save changes</button>
      </div>
      {err && <div className="banner-err">{err}</div>}

      <div className="card">
        <div className="card-head"><h2>Run concurrency</h2></div>
        <div className="row" style={{ alignItems: "flex-start" }}>
          <div className="field" style={{ maxWidth: 160 }}>
            <label>Max concurrent runs</label>
            <input type="number" min={1} max={16} value={concurrency}
              onChange={(e) => setConcurrency(Math.max(1, Math.min(16, Number(e.target.value) || 1)))} />
          </div>
          <p className="subtle" style={{ flex: 1, fontSize: 12.5, lineHeight: 1.6 }}>
            How many benchmark runs the worker executes <b>at the same time</b>. Each run drives a separate
            target cluster, so raise this only when you benchmark several clusters concurrently from this one
            droplet. Higher values multiply load-generator CPU/network use and database connections — make sure
            the droplet and each target can handle it. <b>1</b> (default) runs one benchmark at a time and queues
            the rest. Range 1–16. A change takes effect on the next job the worker claims; in-flight runs are
            unaffected.
          </p>
        </div>
      </div>

      <div className="card">
        <div className="card-head"><h2>Email notifications (SMTP)</h2>
          {s.has_smtp_pw && <span className="chip">password set</span>}</div>
        <div className="row">
          <div className="field"><label>Host</label><input value={smtp.host} onChange={setField("host")} placeholder="smtp.example.com" /></div>
          <div className="field" style={{ maxWidth: 120 }}><label>Port</label><input type="number" value={smtp.port} onChange={setField("port")} /></div>
        </div>
        <div className="row">
          <div className="field"><label>From</label><input value={smtp.from} onChange={setField("from")} placeholder="pgbench@example.com" /></div>
          <div className="field"><label>To (comma-separated)</label><input value={smtp.to} onChange={setField("to")} /></div>
        </div>
        <div className="row">
          <div className="field"><label>Username</label><input value={smtp.user} onChange={setField("user")} /></div>
          <div className="field"><label>Password {s.has_smtp_pw ? "(leave blank to keep)" : ""}</label>
            <input type="password" value={smtpPw} onChange={(e) => setSmtpPw(e.target.value)} autoComplete="off" /></div>
        </div>
        <label className="follow"><input type="checkbox" checked={smtp.tls} onChange={(e) => setSmtp({ ...smtp, tls: e.target.checked })} /> use STARTTLS</label>
      </div>

      <div className="card">
        <div className="card-head"><h2>Slack notifications</h2>{s.has_slack && <span className="chip">webhook set</span>}</div>
        <label className="follow"><input type="checkbox" checked={slackEnabled} onChange={(e) => setSlackEnabled(e.target.checked)} /> enabled</label>
        <div className="field"><label>Webhook URL {s.has_slack ? "(leave blank to keep)" : ""}</label>
          <input type="password" value={slackHook} onChange={(e) => setSlackHook(e.target.value)} autoComplete="off" placeholder="https://hooks.slack.com/…" /></div>
        <button onClick={sendTest}>Send test notification</button> <span className="subtle mono" style={{ fontSize: 12 }}>{testOut}</span>
      </div>

      <div className="card">
        <div className="card-head"><h2>DigitalOcean provider metrics</h2>{s.has_do_token && <span className="chip">token set</span>}</div>
        <p className="subtle" style={{ marginTop: -6, marginBottom: 10, fontSize: 12.5 }}>
          Optional. With a DO API token and the managed-database cluster id, the console fetches device-side
          metrics for a run's window to complement the engine-side IOPS proxy.
        </p>
        <div className="row">
          <div className="field"><label>Cluster id</label><input value={doCluster} onChange={(e) => setDoCluster(e.target.value)} /></div>
          <div className="field"><label>API token {s.has_do_token ? "(leave blank to keep)" : ""}</label>
            <input type="password" value={doToken} onChange={(e) => setDoToken(e.target.value)} autoComplete="off" /></div>
        </div>
        <div className="field"><label>Console base URL (for links in notifications)</label>
          <input value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} placeholder="https://10.0.0.5:8443" /></div>
      </div>
    </>
  );
}
