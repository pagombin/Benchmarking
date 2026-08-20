import { useEffect, useState } from "react";
import { NavLink } from "react-router-dom";
import type { Me, WorkerStatus } from "../types";
import { api, csrfToken } from "../api";
import { useTheme } from "../lib/theme";
import { Palette } from "./Palette";

// Grouped sidebar navigation — enterprise-console IA: the workloads you run,
// the fleet you run them against, what you observe, then administration.
interface NavItem { to: string; label: string; icon: string; admin?: boolean; op?: boolean }
interface NavGroup { title: string; items: NavItem[] }

const GROUPS: NavGroup[] = [
  {
    title: "Workloads",
    items: [
      { to: "/", label: "Runs", icon: "▤" },
      { to: "/continuous", label: "Continuous", icon: "∞" },
      { to: "/new", label: "New run", icon: "＋" },
      { to: "/tasks", label: "Tasks", icon: "☰" },
    ],
  },
  {
    title: "Fleet",
    items: [
      { to: "/targets", label: "DB targets", icon: "⛁" },
      { to: "/ops", label: "Clusters", icon: "⬡" },
      { to: "/ops/runs", label: "Ops runs", icon: "◷" },
    ],
  },
  {
    title: "Observability",
    items: [
      { to: "/alerts", label: "Alerts", icon: "!" },
      { to: "/outages", label: "Outages", icon: "◍" },
      { to: "/compare", label: "Compare", icon: "⇄" },
      { to: "/diagnostics", label: "Environment", icon: "✓", op: true },
    ],
  },
  {
    title: "Administration",
    items: [
      { to: "/users", label: "Users", icon: "◉", admin: true },
      { to: "/settings", label: "Settings", icon: "⚙", admin: true },
      { to: "/audit", label: "Audit", icon: "≡", admin: true },
    ],
  },
];

/** Slim system chip: is the platform itself up (web = this page loaded;
 *  worker = the flock probe), and what is the queue doing. */
function SystemChip() {
  const [ws, setWs] = useState<WorkerStatus | null>(null);
  const [err, setErr] = useState(false);
  useEffect(() => {
    let alive = true;
    const poll = () =>
      api.get<WorkerStatus>("/api/worker/status")
        .then((d) => { if (alive) { setWs(d); setErr(false); } })
        .catch(() => { if (alive) setErr(true); });
    poll();
    const t = setInterval(poll, 30000);
    return () => { alive = false; clearInterval(t); };
  }, []);
  if (err) return <span className="badge failed" title="status probe failed">console degraded</span>;
  if (!ws) return null;
  if (!ws.worker_alive) {
    return <span className="badge failed"
                 title="no process holds the worker lock — queued jobs will not start">
      worker down
    </span>;
  }
  const busy = ws.active_jobs > 0 || ws.queued_jobs > 0;
  return (
    <span className={`badge ${busy ? "running" : "complete"}`}
          title={`worker alive · ${ws.active_jobs} active / ${ws.queued_jobs} queued`}>
      worker ok{busy ? ` · ${ws.active_jobs} active` : ""}
    </span>
  );
}

export function Shell({ me, children }: { me: Me; children: React.ReactNode }) {
  const [theme, toggle] = useTheme();
  const [navOpen, setNavOpen] = useState(false);
  const groups = GROUPS.map((g) => ({
    ...g,
    items: g.items.filter((n) =>
      (!n.admin || me.role === "admin") && (!n.op || me.role !== "viewer")),
  })).filter((g) => g.items.length > 0);

  return (
    <div className="app shell-side">
      <aside className={`sidenav ${navOpen ? "open" : ""}`}>
        <div className="wordmark">
          pgbench<span className="tick">/</span><span className="dim">harness</span>
        </div>
        <button
          className="palette-hint"
          onClick={() => window.dispatchEvent(new KeyboardEvent("keydown", { key: "k", metaKey: true }))}
        >
          <span>Search…</span><kbd>⌘K</kbd>
        </button>
        <nav className="sidenav-groups" onClick={() => setNavOpen(false)}>
          {groups.map((g) => (
            <div className="nav-group" key={g.title}>
              <div className="nav-title">{g.title}</div>
              {g.items.map((n) => (
                <NavLink key={n.to} to={n.to} end={n.to === "/" || n.to === "/ops"}
                         className={({ isActive }) => (isActive ? "active" : "")}>
                  <span className="ni">{n.icon}</span>{n.label}
                </NavLink>
              ))}
            </div>
          ))}
        </nav>
        <div className="sidenav-foot">
          <div className="who">
            <span className={`role role-${me.role}`}>{me.role}</span>
            <span className="mono">{me.user}</span>
          </div>
          <div className="who">
            <button className="ghost" onClick={toggle} title="Toggle theme" aria-label="Toggle theme">
              {theme === "dark" ? "☀ light" : "☾ dark"}
            </button>
            <form method="post" action="/logout" style={{ display: "inline" }}>
              <input type="hidden" name="csrf_token" value={csrfToken()} />
              <button className="ghost" type="submit">Log out</button>
            </form>
          </div>
        </div>
      </aside>

      <div className="shell-main">
        <header className="topbar slim">
          <button className="ghost nav-burger" onClick={() => setNavOpen((v) => !v)}
                  aria-label="Toggle navigation">☰</button>
          <div className="wordmark small">
            pgbench<span className="tick">/</span><span className="dim">harness</span>
          </div>
          <div className="spacer" />
          <SystemChip />
          <span className="subtle" style={{ fontSize: 12, marginLeft: 10 }}>times are UTC</span>
        </header>
        <main className="page">{children}</main>
        <footer className="foot">
          pgbench-harness {me.version}{me.sha ? ` · ${me.sha}` : ""} · self-signed TLS — verify the fingerprint shown at install
        </footer>
      </div>

      <Palette me={me} />
    </div>
  );
}
