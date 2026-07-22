"""Cross-run comparison report.

Overlaid throughput/latency charts, a latency-vs-throughput efficiency view, a
relative-to-baseline view, per-run KPIs with a winner callout, a side-by-side
headline table, and a pg_settings diff (key settings first). Designed for
"Standard vs Advanced" and "tuned vs default" write-ups, and to merge runs
copied from multiple load generators into one results/ folder.
"""

from __future__ import annotations

import csv
import statistics
from pathlib import Path
from typing import Any, Optional

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from pgbench_harness.capture import KEY_SETTINGS, harness_version  # noqa: E402
from pgbench_harness.errors import ReportError  # noqa: E402
from pgbench_harness.manifest import STATUS_OK  # noqa: E402
from pgbench_harness.report import (  # noqa: E402
    FIGSIZE, _jinja_env, _log_x, _style_ax, fig_to_base64, load_pg_settings,
)
from pgbench_harness.util import atomic_write_text, read_json, utc_now_iso  # noqa: E402

# A palette large enough that runs rarely collide; cycles if exceeded.
RUN_COLORS = [
    "#0061eb", "#c0392b", "#1e8449", "#7d3c98", "#b9770e", "#117a8b",
    "#2c3e50", "#d81b60", "#5e8c00", "#00838f",
]


def load_run(run_dir: Path) -> dict[str, Any]:
    """Load one run's summary.json + pg_settings for comparison."""
    summary_path = run_dir / "parsed" / "summary.json"
    if not summary_path.exists():
        raise ReportError(
            f"{run_dir} has no parsed/summary.json",
            hint=f"run `pgbench-harness report --run-dir {run_dir}` first to (re)build it.",
        )
    summary = read_json(summary_path)
    summary["_settings"] = {r["name"]: r["setting"] for r in load_pg_settings(run_dir / "env")}
    summary["_dir"] = str(run_dir)
    return summary


def _disambiguate(runs: list[dict[str, Any]]) -> None:
    """Give each run a unique `display` label (labels can repeat across runs)."""
    seen: dict[str, int] = {}
    for r in runs:
        seen[r["label"]] = seen.get(r["label"], 0) + 1
    for r in runs:
        if seen[r["label"]] > 1:
            suffix = r["run_id"].rsplit("-", 1)[-1]
            r["display"] = f"{r['label']} ({suffix})"
        else:
            r["display"] = r["label"]


def _mean_by_threads(summary: dict[str, Any], metric: str) -> dict[int, float]:
    by_threads: dict[int, list[float]] = {}
    for l in summary["levels"]:
        if l["status"] == STATUS_OK and l.get(metric) is not None:
            by_threads.setdefault(l["threads"], []).append(l[metric])
    return {t: statistics.fmean(vs) for t, vs in sorted(by_threads.items())}


def _color(i: int) -> str:
    return RUN_COLORS[i % len(RUN_COLORS)]


def chart_overlay(runs: list[dict[str, Any]], metric: str, title: str, ylabel: str) -> Optional[str]:
    """Overlaid metric-vs-threads chart, one color per run, legend = run label."""
    fig, ax = plt.subplots(figsize=FIGSIZE)
    all_threads: set[int] = set()
    peak = 0.0
    plotted = False
    for i, run in enumerate(runs):
        pts = _mean_by_threads(run, metric)
        if not pts:
            continue
        ax.plot(list(pts), list(pts.values()), marker="o", linewidth=2.2,
                color=_color(i), label=run["display"])
        all_threads.update(pts)
        peak = max(peak, max(pts.values()))
        plotted = True
    if not plotted:
        plt.close(fig)
        return None
    _style_ax(ax, title, "client threads (log scale)", ylabel)
    _log_x(ax, sorted(all_threads))
    ax.legend(fontsize=11)
    ax.set_ylim(bottom=0, top=peak * 1.18 if peak > 0 else None)
    return fig_to_base64(fig)


def chart_efficiency(runs: list[dict[str, Any]]) -> Optional[str]:
    """Latency-vs-throughput frontier: x = QPS, y = p99, one line per run.

    Lower-and-to-the-right is better (more throughput at less latency). Reveals
    which edition/config sustains lower tail latency at a given load.
    """
    fig, ax = plt.subplots(figsize=FIGSIZE)
    plotted = False
    for i, run in enumerate(runs):
        qps = _mean_by_threads(run, "qps_avg")
        p99 = _mean_by_threads(run, "lat_p99")
        pts = sorted((qps[t], p99[t]) for t in qps if t in p99)
        if not pts:
            continue
        ax.plot([x for x, _ in pts], [y for _, y in pts], marker="o",
                linewidth=2.2, color=_color(i), label=run["display"])
        plotted = True
    if not plotted:
        plt.close(fig)
        return None
    _style_ax(ax, "Latency vs throughput (p99)", "throughput (QPS)", "p99 latency (ms)")
    ax.legend(fontsize=11)
    ax.set_ylim(bottom=0)
    ax.set_xlim(left=0)
    return fig_to_base64(fig)


def chart_relative(runs: list[dict[str, Any]]) -> Optional[str]:
    """QPS of each run relative to the first run (baseline = 100%), vs threads."""
    if len(runs) < 2:
        return None
    base = _mean_by_threads(runs[0], "qps_avg")
    if not base:
        return None
    fig, ax = plt.subplots(figsize=FIGSIZE)
    all_threads: set[int] = set()
    for i, run in enumerate(runs):
        cur = _mean_by_threads(run, "qps_avg")
        pts = sorted((t, cur[t] / base[t] * 100) for t in cur if t in base and base[t] > 0)
        if not pts:
            continue
        ax.plot([t for t, _ in pts], [v for _, v in pts], marker="o",
                linewidth=2.2, color=_color(i), label=run["display"])
        all_threads.update(t for t, _ in pts)
    ax.axhline(100, color="#8b95a3", linewidth=1.2, linestyle="--")
    _style_ax(ax, f"QPS relative to baseline ({runs[0]['display']})",
              "client threads (log scale)", "% of baseline QPS")
    _log_x(ax, sorted(all_threads))
    ax.legend(fontsize=11)
    return fig_to_base64(fig)


def _mean_io_by_threads(summary: dict[str, Any], key: str) -> dict[int, float]:
    by_threads: dict[int, list[float]] = {}
    for l in summary["levels"]:
        io = l.get("io") if l["status"] == STATUS_OK else None
        if io and io.get(key) is not None:
            by_threads.setdefault(l["threads"], []).append(io[key])
    return {t: statistics.fmean(vs) for t, vs in sorted(by_threads.items())}


def chart_io_overlay(runs: list[dict[str, Any]], key: str, title: str, ylabel: str) -> Optional[str]:
    """Overlay one engine-side I/O metric (e.g. write_ops_s) across runs."""
    fig, ax = plt.subplots(figsize=FIGSIZE)
    all_threads: set[int] = set()
    peak = 0.0
    plotted = False
    for i, run in enumerate(runs):
        pts = _mean_io_by_threads(run, key)
        if not pts:
            continue
        ax.plot(list(pts), list(pts.values()), marker="o", linewidth=2.2,
                color=_color(i), label=run["display"])
        all_threads.update(pts)
        peak = max(peak, max(pts.values()))
        plotted = True
    if not plotted:
        plt.close(fig)
        return None
    _style_ax(ax, title, "client threads (log scale)", ylabel)
    _log_x(ax, sorted(all_threads))
    ax.legend(fontsize=11)
    ax.set_ylim(bottom=0, top=peak * 1.18 if peak > 0 else None)
    return fig_to_base64(fig)


def build_run_kpis(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per-run headline KPIs: peak QPS, the thread level at peak, p99 there."""
    kpis = []
    for run in runs:
        qps = _mean_by_threads(run, "qps_avg")
        p99 = _mean_by_threads(run, "lat_p99")
        if qps:
            peak_t = max(qps, key=lambda t: qps[t])
            kpis.append({"display": run["display"], "peak_qps": qps[peak_t],
                         "peak_threads": peak_t, "p99_at_peak": p99.get(peak_t),
                         "status": run["status"]})
        else:
            kpis.append({"display": run["display"], "peak_qps": None,
                         "peak_threads": None, "p99_at_peak": None,
                         "status": run["status"]})
    return kpis


def build_winner(kpis: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Identify the highest-peak-QPS run and its margin over the runner-up."""
    scored = sorted((k for k in kpis if k["peak_qps"] is not None),
                    key=lambda k: k["peak_qps"], reverse=True)
    if not scored:
        return None
    best = scored[0]
    margin = None
    if len(scored) > 1 and scored[1]["peak_qps"] > 0:
        margin = (best["peak_qps"] - scored[1]["peak_qps"]) / scored[1]["peak_qps"] * 100
    return {"display": best["display"], "peak_qps": best["peak_qps"], "margin_pct": margin}


def build_table(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """Side-by-side headline table over the union of thread ladders (gaps allowed).

    For exactly two runs, a Δ QPS % column (second vs first) is included.
    """
    threads = sorted({t for r in runs for t in _mean_by_threads(r, "qps_avg")})
    qps_maps = [_mean_by_threads(r, "qps_avg") for r in runs]
    p99_maps = [_mean_by_threads(r, "lat_p99") for r in runs]
    rows = []
    for t in threads:
        cells = [{"qps": qps_maps[i].get(t), "p99": p99_maps[i].get(t)}
                 for i in range(len(runs))]
        delta = None
        if len(runs) == 2 and cells[0]["qps"] and cells[1]["qps"] is not None:
            delta = (cells[1]["qps"] - cells[0]["qps"]) / cells[0]["qps"] * 100
        rows.append({"threads": t, "cells": cells, "delta_pct": delta})
    return {"threads": threads, "rows": rows, "with_delta": len(runs) == 2}


def settings_diff(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """pg_settings rows that differ between runs, key settings first.

    Each entry: name, per-run values, and whether it's a "key" setting (so the
    template can emphasise the settings our benchmarking rounds focus on).
    """
    all_names = {n for r in runs for n in r["_settings"]}
    key_first = [n for n in KEY_SETTINGS if n in all_names]
    rest = sorted(all_names - set(KEY_SETTINGS))
    diff = []
    for name in key_first + rest:
        values = [r["_settings"].get(name, "—") for r in runs]
        if len(set(values)) > 1:
            diff.append({"name": name, "vals": values, "is_key": name in KEY_SETTINGS})
    return diff


def key_settings_table(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The key settings side-by-side for EVERY run — always shown, identical
    or not. A cross-provider write-up needs the memory/WAL/IO posture of both
    sides on the page even when they happen to match."""
    out = []
    for name in KEY_SETTINGS:
        vals = [r["_settings"].get(name, "n/a") for r in runs]
        out.append({"name": name, "vals": vals,
                    "differs": len({v for v in vals}) > 1})
    return out


def all_settings_union(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """EVERY captured pg_setting side-by-side (union over runs), differing
    rows flagged — the full-capture appendix, rendered collapsed."""
    names = sorted({n for r in runs for n in r["_settings"]})
    return [{"name": n,
             "vals": [r["_settings"].get(n, "—") for r in runs],
             "differs": len({r["_settings"].get(n, "—") for r in runs}) > 1}
            for n in names]


def _first_line(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").splitlines()[0].strip()
    except (OSError, IndexError):
        return ""


def load_env_card(run_dir: Path) -> dict[str, Any]:
    """Per-run identity card: who was tested, with what, when.

    Cross-provider comparisons live or die on this context — 'DO Advanced
    vs Aiven Standard' means nothing without server version, host, workload
    geometry and run window next to the numbers."""
    import yaml as _yaml
    card: dict[str, Any] = {"dir": str(run_dir)}
    try:
        doc = _yaml.safe_load((run_dir / "spec.yaml").read_text(
            encoding="utf-8")) or {}
    except (OSError, _yaml.YAMLError):
        doc = {}
    run = doc.get("run") or {}
    tgt = doc.get("target") or {}
    wl = doc.get("workload") or {}
    card.update({
        "label": run.get("label", ""), "edition": run.get("edition", ""),
        "tshirt_size": run.get("tshirt_size", ""),
        "notes": run.get("notes", ""), "tags": run.get("tags") or [],
        "host": tgt.get("host", ""), "database": tgt.get("database", ""),
        "workload": wl,
        "cluster_aware": "cluster" in doc,
    })
    for section in ("sweep", "soak", "suite"):
        if section in doc:
            card["mode"] = section
            card["mode_cfg"] = doc[section]
            break
    else:
        card["mode"], card["mode_cfg"] = "?", {}
    man = {}
    try:
        man = read_json(run_dir / "manifest.json")
    except (OSError, ValueError):
        pass
    card["run_id"] = man.get("run_id", run_dir.name)
    card["started_utc"] = man.get("created_utc", "")
    card["finished_utc"] = man.get("finished_utc", "")
    card["wall_time_s"] = man.get("wall_time_s")
    card["server_version"] = _first_line(run_dir / "env" / "server_version.txt")
    card["sysbench_version"] = _first_line(run_dir / "env" / "sysbench_version.txt")
    return card


# The fields that make two runs a FAIR comparison. Anything differing here
# is flagged loudly — a DO-vs-Aiven number where the dataset or ladder
# differs is not a comparison, it's an accident.
_FAIRNESS_FIELDS = (
    ("workload type", lambda c: (c["workload"] or {}).get("type")),
    ("tables", lambda c: (c["workload"] or {}).get("tables")),
    ("table_size", lambda c: (c["workload"] or {}).get("table_size")),
    ("scale", lambda c: (c["workload"] or {}).get("scale")),
    ("dataset_gb", lambda c: (c["workload"] or {}).get("dataset_gb")),
    ("mode", lambda c: c.get("mode")),
    ("threads / ladder", lambda c: str((c.get("mode_cfg") or {}).get("threads", ""))),
    ("duration_s", lambda c: (c.get("mode_cfg") or {}).get("duration_s")),
    ("warmup_s", lambda c: (c.get("mode_cfg") or {}).get("warmup_s")),
    ("repetitions", lambda c: (c.get("mode_cfg") or {}).get("repetitions")),
    ("rate_steps", lambda c: str((c.get("mode_cfg") or {}).get("rate_steps", ""))),
    ("sysbench version", lambda c: c.get("sysbench_version")),
    ("PostgreSQL major", lambda c: (c.get("server_version") or "").split(".")[0]
                                   .replace("PostgreSQL ", "").strip()),
)


def comparability(cards: list[dict[str, Any]]) -> dict[str, Any]:
    """Fairness check across the runs: which comparison-critical knobs are
    identical, which differ. `fair` is True only when every field matches."""
    rows = []
    for name, get in _FAIRNESS_FIELDS:
        vals = [get(c) for c in cards]
        shown = ["—" if v in (None, "", "None") else str(v) for v in vals]
        if all(s == "—" for s in shown):
            continue                     # field not used by any run
        rows.append({"name": name, "vals": shown,
                     "same": len(set(shown)) == 1})
    mismatches = [r for r in rows if not r["same"]]
    return {"rows": rows, "mismatches": mismatches,
            "fair": not mismatches}


def _run_mode(run_dir: Path) -> str:
    """sweep | soak, decided by the parsed artifact present (then the manifest)."""
    if (run_dir / "parsed" / "soak_summary.json").exists():
        return "soak"
    if (run_dir / "parsed" / "summary.json").exists():
        return "sweep"
    man = run_dir / "manifest.json"
    if man.exists():
        try:
            return str(read_json(man).get("mode", "sweep"))
        except (ValueError, OSError):
            pass
    return "sweep"


def compare_runs(run_dirs: list[Path], out_path: Path) -> Path:
    """Top-level compare: enforce same run type, then dispatch sweep vs soak.

    Comparing a sweep against a soak is meaningless (one is throughput-vs-threads,
    the other fixed-concurrency-vs-time), so mixed selections are refused with a
    clear message instead of a broken or misleading report.
    """
    if len(run_dirs) < 2:
        raise ReportError("compare needs at least two runs",
                          hint="select two or more runs.")
    modes = {_run_mode(d) for d in run_dirs}
    if len(modes) > 1:
        raise ReportError(
            "cannot compare runs of different types (" + ", ".join(sorted(modes)) + ")",
            hint="select runs of the SAME type — all sweeps, or all soaks.")
    if "soak" in modes:
        return generate_soak_compare(run_dirs, out_path)
    return generate_compare(run_dirs, out_path)


def generate_compare(run_dirs: list[Path], out_path: Path) -> Path:
    """Render the comparison report for N sweep run directories."""
    if len(run_dirs) < 2:
        raise ReportError("compare needs at least two run directories",
                          hint="pass two or more --runs.")
    runs = [load_run(d) for d in run_dirs]
    _disambiguate(runs)
    kpis = build_run_kpis(runs)
    cards = [load_env_card(d) for d in run_dirs]
    for card, run in zip(cards, runs):
        card["display"] = run["display"]
    html = _jinja_env().get_template("compare.html.j2").render(
        runs=runs,
        kpis=kpis,
        winner=build_winner(kpis),
        cards=cards,
        fairness=comparability(cards),
        charts={
            "qps": chart_overlay(runs, "qps_avg", "QPS vs client threads", "QPS"),
            "tps": chart_overlay(runs, "tps_avg", "TPS vs client threads", "TPS"),
            "p99": chart_overlay(runs, "lat_p99", "p99 latency vs client threads", "p99 latency (ms)"),
            "efficiency": chart_efficiency(runs),
            "relative": chart_relative(runs),
            "io_write": chart_io_overlay(runs, "write_ops_s", "Write ops/s vs client threads", "write ops / second"),
            "io_read": chart_io_overlay(runs, "read_ops_s", "Read ops/s vs client threads", "read ops / second"),
        },
        table=build_table(runs),
        diff=settings_diff(runs),
        key_settings=key_settings_table(runs),
        all_settings=all_settings_union(runs),
        any_settings=any(r["_settings"] for r in runs),
        generated_utc=utc_now_iso(),
        harness=harness_version(),
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(out_path, html)
    return out_path


# ── soak-vs-soak comparison ──────────────────────────────────────────────────

def load_soak_run(run_dir: Path) -> dict[str, Any]:
    """Load one soak run's soak_summary.json + pg_settings for comparison."""
    p = run_dir / "parsed" / "soak_summary.json"
    if not p.exists():
        raise ReportError(
            f"{run_dir} has no parsed/soak_summary.json",
            hint=f"run `pgbench-harness report --run-dir {run_dir}` first to (re)build it.")
    s = read_json(p)
    s["_settings"] = {r["name"]: r["setting"] for r in load_pg_settings(run_dir / "env")}
    s["_dir"] = str(run_dir)
    return s


def _soak_series(run_dir: str, col: str) -> tuple[list[int], list[float]]:
    path = Path(run_dir) / "parsed" / "soak_timeseries.csv"
    if not path.exists():
        return [], []
    ts: list[int] = []
    vals: list[float] = []
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            try:
                ts.append(int(row["t"]))
                vals.append(float(row[col]))
            except (KeyError, ValueError):
                continue
    return ts, vals


def chart_soak_overlay(runs: list[dict[str, Any]], col: str, title: str,
                       ylabel: str) -> Optional[str]:
    """Overlaid per-second series over time, one line per soak (decimated)."""
    fig, ax = plt.subplots(figsize=(FIGSIZE[0], 4.6))
    plotted = False
    for i, run in enumerate(runs):
        ts, vals = _soak_series(run["_dir"], col)
        if not ts:
            continue
        stride = max(1, len(ts) // 1500)
        ax.plot(ts[::stride], vals[::stride], color=_color(i), linewidth=1.6,
                label=run["display"])
        plotted = True
    if not plotted:
        plt.close(fig)
        return None
    _style_ax(ax, title, "elapsed time (s)", ylabel)
    ax.legend(fontsize=11)
    ax.set_ylim(bottom=0)
    return fig_to_base64(fig)


def chart_soak_throughput(runs: list[dict[str, Any]]) -> Optional[str]:
    return chart_soak_overlay(runs, "tps", "Throughput over time (overlaid)",
                              "TPS")


def build_soak_kpis(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per-soak headline metrics: throughput, tail latency, coverage, downtime."""
    out = []
    for r in runs:
        rp = r.get("run_profile") or {}
        tps = rp.get("tps") or {}
        lat = rp.get("latency_ms") or {}
        out.append({
            "display": r["display"], "status": r.get("status"),
            "threads": (r.get("soak") or {}).get("threads"),
            "median_tps": tps.get("median"), "peak_tps": tps.get("max"),
            "cov_pct": tps.get("cov_pct"),
            "p50": lat.get("p50"), "p95": lat.get("p95"), "p99": lat.get("p99"),
            "coverage_pct": r.get("coverage_pct"),
            "longest_outage_s": rp.get("longest_outage_s"),
            "events": len(r.get("events") or []), "detected": len(r.get("detected") or []),
        })
    return out


def build_soak_winner(kpis: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    scored = sorted((k for k in kpis if k["median_tps"] is not None),
                    key=lambda k: k["median_tps"], reverse=True)
    if not scored:
        return None
    best = scored[0]
    margin = None
    if len(scored) > 1 and scored[1]["median_tps"] > 0:
        margin = (best["median_tps"] - scored[1]["median_tps"]) / scored[1]["median_tps"] * 100
    return {"display": best["display"], "median_tps": best["median_tps"], "margin_pct": margin}


def generate_soak_compare(run_dirs: list[Path], out_path: Path) -> Path:
    """Render the comparison report for N soak run directories."""
    if len(run_dirs) < 2:
        raise ReportError("compare needs at least two run directories",
                          hint="pass two or more --runs.")
    runs = [load_soak_run(d) for d in run_dirs]
    _disambiguate(runs)
    kpis = build_soak_kpis(runs)
    cards = [load_env_card(d) for d in run_dirs]
    for card, run in zip(cards, runs):
        card["display"] = run["display"]
    # two-run comparisons get a delta column (second vs first)
    deltas = None
    if len(kpis) == 2 and kpis[0].get("median_tps") and \
            kpis[1].get("median_tps") is not None:
        base, other = kpis[0], kpis[1]
        def _pct(a, b):
            return (b - a) / a * 100 if (a and b is not None) else None
        deltas = {"median_tps": _pct(base["median_tps"], other["median_tps"]),
                  "p99": _pct(base["p99"], other["p99"]) if base.get("p99") else None}
    html = _jinja_env().get_template("compare_soak.html.j2").render(
        runs=runs,
        kpis=kpis,
        winner=build_soak_winner(kpis),
        deltas=deltas,
        cards=cards,
        fairness=comparability(cards),
        chart=chart_soak_throughput(runs),
        chart_lat=chart_soak_overlay(runs, "lat_p99",
                                     "p99 latency over time (overlaid)",
                                     "p99 latency (ms)"),
        chart_err=chart_soak_overlay(runs, "err_s",
                                     "Errors/s over time (overlaid)",
                                     "errors / second"),
        diff=settings_diff(runs),
        key_settings=key_settings_table(runs),
        all_settings=all_settings_union(runs),
        any_settings=any(r["_settings"] for r in runs),
        generated_utc=utc_now_iso(),
        harness=harness_version(),
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(out_path, html)
    return out_path
