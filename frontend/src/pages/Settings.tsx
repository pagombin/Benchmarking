import { useEffect, useState } from "react";
import { api } from "../api";
import { usePageTitle } from "../lib/ui";

interface SmtpCfg { host: string; port: number; user: string; from: string; to: string; tls: boolean; }
interface AlertsCfg { [k: string]: number }
interface ChannelResult { configured: boolean; ok: boolean; error: string }
interface SettingsResp {
  notify: { smtp?: Partial<SmtpCfg>; slack?: { enabled?: boolean } };
  base_url: string;
  do_cluster_id: string;
  max_concurrency: number;
  continuous_cap: number;
  heartbeat_url: string;
  cont_alerts_config: AlertsCfg;
  cont_retention_raw_h: number;
  cont_retention_days: number;
  has_smtp_pw: boolean;
  has_slack: boolean;
  has_do_token: boolean;
}

// Friendly labels + units for the continuous alert thresholds (the raw JSON
// lives in the cont_alerts_config setting; this form edits it field by field).
const THRESHOLDS: { key: string; label: string; unit: string }[] = [
  { key: "probe_interval_s", label: "Probe interval", unit: "s" },
  { key: "probe_failures_to_down", label: "Failures → down", unit: "probes" },
  { key: "load_gap_s", label: "Load-gap outage after", unit: "s" },
  { key: "err_rate_threshold", label: "Error-rate alert above", unit: "err/s" },
  { key: "err_rate_hold_s", label: "… sustained for", unit: "s" },
  { key: "latency_p99_ms", label: "Latency alert above", unit: "ms p99" },
  { key: "latency_hold_s", label: "… sustained for", unit: "s" },
  { key: "tps_drop_pct", label: "TPS-drop alert below", unit: "% of 24h median" },
  { key: "renotify_min", label: "Re-notify standing crits every", unit: "min" },
  { key: "disk_warn_pct", label: "Loadgen disk warning at", unit: "% used" },
  { key: "no_data_s", label: "No-data alert after", unit: "s" },
];

// Shipped defaults, kept in lock-step with contprobe.ALERTS_CONFIG_DEFAULTS —
// "Restore defaults" fills the form; nothing is persisted until Save.
const THRESHOLD_DEFAULTS: AlertsCfg = {
  probe_interval_s: 5, probe_failures_to_down: 3, load_gap_s: 30,
  err_rate_threshold: 1.0, err_rate_hold_s: 60,
  latency_p99_ms: 500, latency_hold_s: 300, tps_drop_pct: 50,
  renotify_min: 60, disk_warn_pct: 85, no_data_s: 120,
};

export function Settings() {
  usePageTitle("Settings");
  const [s, setS] = useState<SettingsResp | null>(null);
  const [smtp, setSmtp] = useState<SmtpCfg>({ host: "", port: 587, user: "", from: "", to: "", tls: true });
  const [slackEnabled, setSlackEnabled] = useState(false);
  const [concurrency, setConcurrency] = useState(1);
  const [contCap, setContCap] = useState(4);
  const [heartbeat, setHeartbeat] = useState("");
  const [alertsCfg, setAlertsCfg] = useState<AlertsCfg>({});
  const [retRawH, setRetRawH] = useState(72);
  const [retDays, setRetDays] = useState(35);
  const [doCluster, setDoCluster] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [smtpPw, setSmtpPw] = useState("");
  const [slackHook, setSlackHook] = useState("");
  const [doToken, setDoToken] = useState("");
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [testText, setTestText] = useState("");
  const [testOut, setTestOut] = useState<Record<string, ChannelResult> | string>("");

  function load() {
    api.get<SettingsResp>("/api/admin/settings").then((d) => {
      setS(d);
      setSmtp({ host: d.notify.smtp?.host ?? "", port: d.notify.smtp?.port ?? 587, user: d.notify.smtp?.user ?? "",
        from: d.notify.smtp?.from ?? "", to: d.notify.smtp?.to ?? "", tls: d.notify.smtp?.tls ?? true });
      setSlackEnabled(!!d.notify.slack?.enabled);
      setConcurrency(d.max_concurrency);
      setContCap(d.continuous_cap);
      setHeartbeat(d.heartbeat_url);
      setAlertsCfg(d.cont_alerts_config);
      setRetRawH(d.cont_retention_raw_h);
      setRetDays(d.cont_retention_days);
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
        continuous_cap: contCap, heartbeat_url: heartbeat,
        cont_alerts_config: alertsCfg,
        cont_retention_raw_h: retRawH, cont_retention_days: retDays,
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
      const d = await api.post<{ sent: string[]; channels: Record<string, ChannelResult> }>(
        "/api/notify/test", testText ? { text: testText } : {});
      setTestOut(d.channels);
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
          <div className="field" style={{ maxWidth: 200 }}>
            <label>Max continuous workloads</label>
            <input type="number" min={1} max={16} value={contCap}
              onChange={(e) => setContCap(Math.max(1, Math.min(16, Number(e.target.value) || 1)))} />
          </div>
          <p className="subtle" style={{ flex: 1, fontSize: 12.5, lineHeight: 1.6 }}>
            Benchmark runs and continuous (24/7) workloads run in <b>separate lanes</b>: a month-long
            continuous workload never occupies a benchmark slot and vice versa. Each lane has its own
            cap (1–16). Changes apply to the next job the worker claims.
          </p>
        </div>
      </div>

      <div className="card">
        <div className="card-head"><h2>Continuous mode — alert thresholds</h2>
          <button className="btn-sm"
            onClick={() => setAlertsCfg({ ...alertsCfg, ...THRESHOLD_DEFAULTS })}
            title="Reset the fields below to the shipped defaults (takes effect on Save)">
            Restore defaults
          </button></div>
        <p className="subtle" style={{ marginTop: -6, marginBottom: 10, fontSize: 12.5 }}>
          Drives the availability prober, the outage ledger, and Slack alerting for continuous
          workloads. Every alert is stored in the history whether or not delivery succeeds.
        </p>
        <div className="row" style={{ flexWrap: "wrap" }}>
          {THRESHOLDS.map(({ key, label, unit }) => (
            <div className="field" key={key} style={{ maxWidth: 190 }}>
              <label>{label} <span className="subtle">({unit})</span></label>
              <input type="number" min={0} step="any" value={alertsCfg[key] ?? ""}
                onChange={(e) => setAlertsCfg({ ...alertsCfg, [key]: Number(e.target.value) })} />
            </div>
          ))}
        </div>
        <div className="row" style={{ marginTop: 6 }}>
          <div className="field" style={{ maxWidth: 190 }}>
            <label>Raw sample retention <span className="subtle">(hours)</span></label>
            <input type="number" min={1} value={retRawH}
              onChange={(e) => setRetRawH(Number(e.target.value) || 72)} />
          </div>
          <div className="field" style={{ maxWidth: 190 }}>
            <label>History retention <span className="subtle">(days)</span></label>
            <input type="number" min={1} value={retDays}
              onChange={(e) => setRetDays(Number(e.target.value) || 35)} />
          </div>
          <div className="field" style={{ flex: 1 }}>
            <label>Dead-man heartbeat URL <span className="subtle">(healthchecks.io-style; pinged every 60s while a workload runs — the external service alerts when pings STOP, e.g. the droplet powered off)</span></label>
            <input value={heartbeat} onChange={(e) => setHeartbeat(e.target.value)}
              placeholder="https://hc-ping.com/<uuid> (empty = disabled)" />
          </div>
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
        <div className="row" style={{ alignItems: "flex-end" }}>
          <div className="field" style={{ flex: 1 }}>
            <label>Test message text (optional)</label>
            <input value={testText} onChange={(e) => setTestText(e.target.value)}
              placeholder="This is a test notification…" />
          </div>
          <button onClick={sendTest} style={{ marginBottom: 8 }}>Send test message</button>
        </div>
        {typeof testOut === "string" ? (
          testOut && <span className="subtle mono" style={{ fontSize: 12 }}>{testOut}</span>
        ) : (
          <table style={{ marginTop: 6 }}>
            <thead><tr><th>Channel</th><th>Configured</th><th>Result</th><th>Error</th></tr></thead>
            <tbody>
              {Object.entries(testOut).map(([ch, r]) => (
                <tr key={ch}>
                  <td className="mono">{ch}</td>
                  <td>{r.configured ? "yes" : "no"}</td>
                  <td>{!r.configured ? "—" : r.ok
                    ? <span className="badge complete">sent</span>
                    : <span className="badge failed">failed</span>}</td>
                  <td className="subtle" style={{ fontSize: 12 }}>{r.error}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
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
