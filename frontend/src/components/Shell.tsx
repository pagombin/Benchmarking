import { NavLink } from "react-router-dom";
import type { Me } from "../types";
import { csrfToken } from "../api";
import { useTheme } from "../lib/theme";

// During the migration some destinations are still server-rendered Jinja pages.
// `external: true` routes do a full-page navigation; the rest are SPA routes.
// As each phase lands, flip `external` off and point at the SPA path.
// Internal routes are basename-relative (the router's basename is /ui); external
// destinations are still server-rendered Jinja pages reached by full-page nav.
const NAV: { to: string; label: string; external?: boolean; admin?: boolean }[] = [
  { to: "/", label: "Runs" },
  { to: "/new", label: "New run" },
  { to: "/targets", label: "Targets" },
  { to: "/compare", label: "Compare", external: true },
  { to: "/admin/users", label: "Users", external: true, admin: true },
  { to: "/admin/settings", label: "Settings", external: true, admin: true },
  { to: "/audit", label: "Audit", external: true, admin: true },
];

export function Shell({ me, children }: { me: Me; children: React.ReactNode }) {
  const [theme, toggle] = useTheme();
  const items = NAV.filter((n) => !n.admin || me.role === "admin");
  return (
    <div className="app">
      <header className="topbar">
        <div className="wordmark">
          pgbench<span className="tick">/</span><span className="dim">harness</span>
        </div>
        <nav className="topnav">
          {items.map((n) =>
            n.external ? (
              <a key={n.to} href={n.to}>{n.label}</a>
            ) : (
              <NavLink key={n.to} to={n.to} end className={({ isActive }) => (isActive ? "active" : "")}>
                {n.label}
              </NavLink>
            )
          )}
        </nav>
        <div className="spacer" />
        <div className="who">
          <span className={`role role-${me.role}`}>{me.role}</span>
          <span className="mono">{me.user}</span>
          <form method="post" action="/logout" style={{ display: "inline" }}>
            <input type="hidden" name="csrf_token" value={csrfToken()} />
            <button className="ghost" type="submit">Log out</button>
          </form>
          <button className="ghost" onClick={toggle} title="Toggle theme" aria-label="Toggle theme">
            {theme === "dark" ? "☀" : "☾"}
          </button>
        </div>
      </header>
      <main className="page">{children}</main>
      <footer className="foot">
        pgbench-harness {me.version} · self-signed TLS — verify the fingerprint shown at install · times are UTC
      </footer>
    </div>
  );
}
