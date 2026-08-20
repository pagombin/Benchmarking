import { useEffect, useRef } from "react";
import uPlot from "uplot";
import "uplot/dist/uPlot.min.css";

// Continuous-view chart: LiveChart's shape plus shaded time BANDS (outages /
// maintenance windows) and vertical event markers, on an absolute epoch-seconds
// x-axis rendered as UTC clock time.

export interface ContChartSeries {
  label: string;
  values: (number | null)[];
  stroke: string;
  scale?: string;
  fill?: string;
}

export interface Band {
  from: number;                 // epoch s
  to: number;                   // epoch s
  color: string;                // rgba fill
  label?: string;
}

export interface VMarker {
  t: number;                    // epoch s
  label: string;
  color?: string;
}

interface Props {
  title: string;
  xs: number[];
  series: ContChartSeries[];
  height?: number;
  yFormat?: (v: number) => string;
  bands?: Band[];
  markers?: VMarker[];
  spanGaps?: boolean;
}

const AXIS = "#8b97a6";
const GRID = "rgba(139,151,166,0.16)";
const MARKER = "#f85149";

const clean = (vals: (number | null)[]): (number | null)[] =>
  vals.map((v) => (v !== null && Number.isFinite(v) ? v : null));

/** epoch seconds -> "HH:MM" or "MMM d HH:MM" (UTC) depending on span */
export function fmtClock(epoch: number, spanS: number): string {
  const d = new Date(epoch * 1000);
  const hh = String(d.getUTCHours()).padStart(2, "0");
  const mm = String(d.getUTCMinutes()).padStart(2, "0");
  if (spanS <= 48 * 3600) return `${hh}:${mm}`;
  const mon = d.toLocaleString("en-US", { month: "short", timeZone: "UTC" });
  return `${mon} ${d.getUTCDate()} ${hh}:${mm}`;
}

export function ContChart({ title, xs, series, height = 220, yFormat,
                            bands, markers, spanGaps }: Props) {
  const host = useRef<HTMLDivElement>(null);
  const plot = useRef<uPlot | null>(null);
  const bandsRef = useRef<Band[]>(bands ?? []);
  const markersRef = useRef<VMarker[]>(markers ?? []);
  bandsRef.current = bands ?? [];
  markersRef.current = markers ?? [];

  useEffect(() => {
    if (!host.current) return;
    const el = host.current;
    const fmt = (vals: number[]) => (yFormat ? vals.map((v) => yFormat(v)) : vals.map(String));
    const span = xs.length > 1 ? xs[xs.length - 1] - xs[0] : 3600;
    const hasRight = series.some((s) => s.scale === "y2");
    const axes: uPlot.Axis[] = [
      {
        stroke: AXIS, grid: { stroke: GRID, width: 1 }, ticks: { stroke: GRID },
        values: (_u, vals) => vals.map((v) => fmtClock(v, span)),
        font: "11px 'IBM Plex Mono', monospace",
      },
      {
        scale: "y", stroke: AXIS, grid: { stroke: GRID, width: 1 }, ticks: { stroke: GRID },
        values: (_u, vals) => fmt(vals), font: "11px 'IBM Plex Mono', monospace", size: 56,
      },
    ];
    if (hasRight) {
      axes.push({
        scale: "y2", side: 1, stroke: AXIS, grid: { show: false }, ticks: { stroke: GRID },
        values: (_u, vals) => fmt(vals), font: "11px 'IBM Plex Mono', monospace", size: 56,
      });
    }

    // Bands under the series, markers over them.
    const drawBands = (u: uPlot) => {
      const c = u.ctx;
      c.save();
      for (const b of bandsRef.current) {
        const x0 = Math.max(u.bbox.left, u.valToPos(b.from, "x", true));
        const x1 = Math.min(u.bbox.left + u.bbox.width, u.valToPos(b.to, "x", true));
        if (!(x1 > u.bbox.left && x0 < u.bbox.left + u.bbox.width)) continue;
        c.fillStyle = b.color;
        c.fillRect(x0, u.bbox.top, Math.max(2, x1 - x0), u.bbox.height);
      }
      c.restore();
    };
    const drawMarkers = (u: uPlot) => {
      const c = u.ctx;
      c.save();
      c.font = "10px 'IBM Plex Mono', monospace";
      c.textBaseline = "top";
      let li = 0;
      for (const m of [...markersRef.current].sort((a, b) => a.t - b.t)) {
        const x = Math.round(u.valToPos(m.t, "x", true));
        if (x < u.bbox.left || x > u.bbox.left + u.bbox.width) continue;
        c.strokeStyle = m.color ?? MARKER;
        c.lineWidth = 1;
        c.setLineDash([4, 3]);
        c.beginPath(); c.moveTo(x, u.bbox.top); c.lineTo(x, u.bbox.top + u.bbox.height); c.stroke();
        if (height >= 200 && m.label) {
          c.setLineDash([]);
          c.fillStyle = m.color ?? MARKER;
          c.fillText(m.label, x + 4, u.bbox.top + 2 + (li % 4) * 12);
          li++;
        }
      }
      c.restore();
    };

    const opts: uPlot.Options = {
      title,
      width: el.clientWidth || 600,
      height,
      cursor: { drag: { x: true, y: false } },
      legend: { live: true },
      scales: {
        x: { time: false },
        y: { range: (_u, min, max) => (min === max ? [0, max || 1] : [Math.min(0, min), max]) },
        y2: { range: (_u, min, max) => (min === max ? [0, max || 1] : [Math.min(0, min), max]) },
      },
      axes,
      series: [
        {},
        ...series.map((s) => ({
          label: s.label, stroke: s.stroke, width: 1.6, fill: s.fill,
          scale: s.scale ?? "y", points: { show: false }, spanGaps: !!spanGaps,
        })),
      ],
      hooks: { drawClear: [drawBands], draw: [drawMarkers] },
    };
    const data: uPlot.AlignedData = [xs, ...series.map((s) => clean(s.values))] as uPlot.AlignedData;
    const u = new uPlot(opts, data, el);
    plot.current = u;
    const ro = new ResizeObserver(() => u.setSize({ width: el.clientWidth, height }));
    ro.observe(el);
    return () => {
      ro.disconnect();
      u.destroy();
      plot.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [title, height, series.map((s) => `${s.label}:${s.scale ?? "y"}`).join("|")]);

  useEffect(() => {
    if (plot.current) {
      plot.current.setData([xs, ...series.map((s) => clean(s.values))] as uPlot.AlignedData);
    }
  }, [xs, series]);

  useEffect(() => { plot.current?.redraw(); }, [bands, markers]);

  return <div className="chart" ref={host} />;
}
