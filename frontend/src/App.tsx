import { useEffect, useState } from "react";
import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import { api } from "./api";
import type { Me } from "./types";
import { Shell } from "./components/Shell";
import { History } from "./pages/History";
import { RunDetail } from "./pages/RunDetail";
import { ReportView } from "./pages/ReportView";
import { Targets } from "./pages/Targets";
import { NewRun } from "./pages/NewRun";

export function App() {
  const [me, setMe] = useState<Me | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    api.get<Me>("/api/me").then(setMe).catch((e) => setErr(e.message));
  }, []);

  if (err) {
    return (
      <div className="login-wrap">
        <div className="card login-card">
          <h1>Console unavailable</h1>
          <p className="subtle">{err}</p>
          <a className="btn" href="/login">Go to sign in</a>
        </div>
      </div>
    );
  }
  if (!me) {
    return <div className="login-wrap"><div className="subtle mono">loading console…</div></div>;
  }

  return (
    <BrowserRouter basename="/ui">
      <Shell me={me}>
        <Routes>
          <Route path="/" element={<History me={me} />} />
          <Route path="/new" element={<NewRun me={me} />} />
          <Route path="/targets" element={<Targets me={me} />} />
          <Route path="/runs/:runId" element={<RunDetail me={me} />} />
          <Route path="/runs/:runId/report" element={<ReportView />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </Shell>
    </BrowserRouter>
  );
}
