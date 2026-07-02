import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api } from "../api";
import type { Run } from "../types";
import { LiveChart } from "../components/LiveChart";
import { fmtCompact, fmtInt, fmtNum } from "../lib/format";
import { appendBatch, emptySeries, openStream, type Series } from "../lib/sse";

// Two clearly-distinct hues, one per run (A / B), reused across every chart.
const RUN_COLORS = ["#2dd4bf", "#f59e0b"];

interface RunMeta {
  label?: string;
  mode?: string;
  status?: string;
  startUtc?: string;   // wall-clock t=0 anchor from the SSE hello
  budget?: number;     // planned wall-clock seconds
  elapsed?: number;    // live elapsed from progress
  live?: boolean;
}

function hms(v: number): string {
  const s = Math.max(0, Math.round(v));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  const p = (n: number) => String(n).padStart(2, "0");
  return h ? `${h}:${p(m)}:${p(sec)}` : `${m}:${p(sec)}`;
}

// Scatter a run's per-second series onto a shared axis: a sample at run-elapsed
// t lands at unified second (offset + t); everything else is NaN (a chart gap).
function aligned(len: number, offset: number, ts: number[], vals: number[]): number[] {
  const out = new Array(len).fill(NaN);
  for (let i = 0; i < ts.length; i++) {
    const x = offset + ts[i];
    if (x >= 0 && x < len) out[x] = vals[i];
  }
  return out;
}

const epoch = (iso?: string): number => {
  if (!iso) return NaN;
  const t = Date.parse(iso);
  return Number.isNaN(t) ? NaN : t / 1000;
};

export function LiveCompare() {
  const [sp] = useSearchParams();
  const ids = useMemo(
    () => (sp.get("runs") || "").split(",").map((s) => s.trim()).filter(Boolean).slice(0, 2),
    [sp]);
  const [meta, setMeta] = useState<Record<string, RunMeta>>({});
  const seriesRef = useRef<Record<string, Series>>({});
  const [, setNonce] = useState(0);
  const bump = () => setNonce((n) => n + 1);

  // Baseline metadata (label/mode/status) even before the first hello arrives.
  useEffect(() => {
    ids.forEach((id) => {
      api.get<Run>(`/api/runs/${id}`)
        .then((r) => setMeta((m) => ({ ...m, [id]: { ...(m[id] || {}), label: r.label, mode: r.mode, status: m[id]?.status || r.status } })))
        .catch(() => {});
    });
  }, [ids]);

  // One live SSE stream per run, accumulated into its own Series.
  useEffect(() => {
    const closers = ids.map((id) => {
      seriesRef.current[id] = emptySeries();
      const es = openStream(id, {
        onHello: (h) => {
          seriesRef.current[id] = emptySeries();
          setMeta((m) => ({ ...m, [id]: { ...(m[id] || {}), startUtc: h.start_utc, mode: h.mode, status: h.status, budget: h.budget_s, live: true } }));
          bump();
        },
        onSamples: (b) => {
          if (b.offset === 0) seriesRef.current[id] = emptySeries();
          appendBatch(seriesRef.current[id], b);
          bump();
        },
        onProgress: (p) => setMeta((m) => ({ ...m, [id]: { ...(m[id] || {}), elapsed: p.elapsed_s } })),
        onDone: (d) => setMeta((m) => ({ ...m, [id]: { ...(m[id] || {}), status: d.status, live: false } })),
        onError: () => {},
      });
      return () => es.close();
    });
    return () => closers.forEach((c) => c());
  }, [ids]);

  if (ids.length !== 2) {
    return <div className="banner-err">Live compare needs exactly two runs — open it from Compare with two soaks selected.</div>;
  }

  // ── alignment on a shared real-time axis ──────────────────────────────
  const per = ids.map((id, i) => {
    const s = seriesRef.current[id] || emptySeries();
    const lastT = s.t.length ? s.t[s.t.length - 1] : -1;
    const span = Math.max(lastT, meta[id]?.elapsed ?? 0, 0);   // seconds this run has covered
    return { id, i, color: RUN_COLORS[i], s, lastT, span, start: epoch(meta[id]?.startUtc), m: meta[id] || {} };
  });

  const haveStarts = per.every((r) => Number.isFinite(r.start));
  const t0 = haveStarts ? Math.min(...per.map((r) => r.start)) : NaN;
  const laid = per.map((r) => {
    const offset = haveStarts ? Math.round(r.start - t0) : 0;
    const budget = r.m.budget || 0;
    return { ...r, offset, end: offset + r.span, plannedEnd: offset + Math.max(budget, r.span) };
  });
  const unifiedLen = Math.max(1, ...laid.map((r) => r.end + 1));
  const xMax = Math.max(unifiedLen, ...laid.map((r) => r.plannedEnd)) || undefined;
  const xs = Array.from({ length: unifiedLen }, (_, k) => k);
  const short = (r: typeof laid[number]) => r.m.label || r.id.slice(-8);

  const mk = (get: (s: Series) => number[]) =>
    laid.map((r) => ({ label: short(r), values: aligned(unifiedLen, r.offset, r.s.t, get(r.s)), stroke: r.color }));

  // ── overlap detection ─────────────────────────────────────────────────
  const [A, B] = laid;
  const lead = B.offset - A.offset;                       // >0: A started first
  const overlapStart = Math.max(A.offset, B.offset);
  const overlapEnd = Math.min(A.end, B.end);
  const overlap = Math.max(0, overlapEnd - overlapStart);
  const leader = lead === 0 ? null : lead > 0 ? A : B;
  const laggard = leader === A ? B : A;
  const overlapMsg = !haveStarts ? "waiting for both runs to report their start time…"
    : lead === 0 ? "Both runs started at the same second."
      : `${short(leader!)} started ${hms(Math.abs(lead))} before ${short(laggard!)}.`;
  // who ends first (only meaningful once at least one is terminal)
  const endMsg = (() => {
    const aDone = A.m.live === false, bDone = B.m.live === false;
    if (!aDone && !bDone) return `Both running — overlap ${hms(overlap)} and counting.`;
    if (aDone && bDone) {
      const first = A.end <= B.end ? A : B, second = first === A ? B : A;
      return `${short(first)} finished ${hms(Math.abs(B.end - A.end))} before ${short(second)}. Total overlap ${hms(overlap)}.`;
    }
    const done = aDone ? A : B, running = aDone ? B : A;
    return `${short(done)} has finished; ${short(running)} is still running. Overlap so far ${hms(overlap)}.`;
  })();

  const last = (arr: number[]) => (arr.length ? arr[arr.length - 1] : null);
  const peak = (arr: number[]) => (arr.length ? Math.max(...arr) : null);
  const anyData = laid.some((r) => r.s.t.length > 0);
  const notSoak = laid.some((r) => r.m.mode && r.m.mode !== "soak");

  return (
    <>
      <div className="toolbar">
        <div>
          <h1>Live compare</h1>
          <div className="subtle mono" style={{ fontSize: 12 }}>{ids.join("  vs  ")}</div>
        </div>
        <div className="spacer" />
        <Link className="btn" to="/compare">← Compare</Link>
      </div>

      {notSoak && <div className="banner-warn">Live compare aligns runs on wall-clock time and is designed for soaks; a sweep's per-level timeline won't line up cleanly.</div>}

      {/* run header chips + live status */}
      <div className="cmp-heads">
        {laid.map((r) => (
          <div key={r.id} className="cmp-head" style={{ borderLeftColor: r.color }}>
            <span className="dot" style={{ background: r.color }} />
            <b>{short(r)}</b>
            <span className={`badge ${r.m.status || ""}`}>{r.m.status || (r.m.live ? "running" : "—")}</span>
            <span className="subtle mono" style={{ fontSize: 12 }}>{r.m.mode || "—"} · {hms(r.span)}{r.m.budget ? ` / ${hms(r.m.budget)}` : ""}</span>
            <Link className="subtle" to={`/runs/${r.id}`} style={{ fontSize: 12, marginLeft: "auto" }}>open ↗</Link>
          </div>
        ))}
      </div>

      {/* overlap detection */}
      <div className="card overlap">
        <div className="card-head"><h2>Overlap</h2></div>
        <div>{overlapMsg}</div>
        <div>{endMsg}</div>
        {/* timeline strip: each run's active window on the shared axis, overlap shaded */}
        {haveStarts && xMax && (
          <div className="tl-strip" style={{ marginTop: 10 }}>
            {overlap > 0 && (
              <div className="tl-overlap" style={{ left: `${(overlapStart / xMax) * 100}%`, width: `${(overlap / xMax) * 100}%` }} />
            )}
            {laid.map((r) => (
              <div key={r.id} className="tl-row">
                <span className="tl-label mono">{short(r)}</span>
                <div className="tl-track">
                  <div className="tl-bar" style={{ left: `${(r.offset / xMax) * 100}%`, width: `${(Math.max(1, r.end - r.offset) / xMax) * 100}%`, background: r.color, opacity: r.m.live ? 0.9 : 0.5 }} />
                </div>
              </div>
            ))}
            <div className="tl-axis"><span>0:00</span><span>{hms(xMax)}</span></div>
          </div>
        )}
      </div>

      {/* live KPI comparison (delta is B relative to A; colour reflects which is better) */}
      <div className="grid2">
        {([
          { label: "Current TPS", get: (s: Series) => last(s.tps), fmt: fmtInt, lowerBetter: false },
          { label: "Peak TPS", get: (s: Series) => peak(s.tps), fmt: fmtInt, lowerBetter: false },
          { label: "Current p99 (ms)", get: (s: Series) => last(s.p99), fmt: fmtNum, lowerBetter: true },
          { label: "Current QPS", get: (s: Series) => last(s.qps), fmt: fmtInt, lowerBetter: false },
        ]).map(({ label, get, fmt, lowerBetter }) => {
          const va = get(A.s), vb = get(B.s);
          const delta = va != null && vb != null && va !== 0 ? ((vb - va) / va) * 100 : null;
          const good = delta == null ? true : (lowerBetter ? delta < 0 : delta > 0);
          return (
            <div className="card kpi-compare" key={label}>
              <div className="label">{label}</div>
              <div className="cmp-vals">
                <span style={{ color: A.color }}>{fmt(va)}</span>
                <span className="subtle">vs</span>
                <span style={{ color: B.color }}>{fmt(vb)}</span>
                {delta != null && delta !== 0 && (
                  <span className={`delta ${good ? "up" : "down"}`}>{delta >= 0 ? "+" : ""}{delta.toFixed(0)}%</span>
                )}
              </div>
            </div>
          );
        })}
      </div>

      {!anyData ? (
        <div className="card"><div className="subtle mono">waiting for live samples from both runs…</div></div>
      ) : (
        <>
          <div className="card">
            <LiveChart title="Throughput — TPS (aligned on real time)" xs={xs} xMax={xMax} xFormat={hms}
              yFormat={(v) => fmtCompact(v)} series={mk((s) => s.tps)} />
          </div>
          <div className="card">
            <LiveChart title="QPS (aligned on real time)" xs={xs} xMax={xMax} xFormat={hms}
              yFormat={(v) => fmtCompact(v)} series={mk((s) => s.qps)} />
          </div>
          <div className="grid2">
            <div className="card">
              <LiveChart title="p99 latency (ms)" xs={xs} xMax={xMax} xFormat={hms} height={200}
                yFormat={(v) => fmtCompact(v)} series={mk((s) => s.p99)} />
            </div>
            <div className="card">
              <LiveChart title="Errors (per second)" xs={xs} xMax={xMax} xFormat={hms} height={200}
                yFormat={(v) => fmtCompact(v)} series={mk((s) => s.err)} />
            </div>
          </div>
        </>
      )}
      <p className="subtle" style={{ fontSize: 12 }}>
        x-axis is elapsed since the earlier run started; each line breaks where that run wasn't producing samples.
        Both streams are live — a run that starts, stops, or gaps shows up against the other in real time.
      </p>
    </>
  );
}
