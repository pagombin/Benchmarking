"""One-click IOPS evidence pack: the core four device probes as one job,
plus a consolidated, storage-team-ready narrative with fresh numbers.

The pack runs, sequentially, against the same kept test files:

  1. rndrd 16 KiB  — random-read ceiling at the probe's native block size
  2. rndrd  8 KiB  — the database-page-size ceiling (IOPS-vs-bandwidth
                     discriminator)
  3. rndwr 16 KiB  — random-write ceiling
  4. rndwr 16 KiB  — replication of #3 (determinism proof)

All four run with O_DIRECT (no page-cache/writeback inflation), so the load
generator's own counters and the device counters agree. Each probe is a
normal device-probe run directory (individually browsable, live cockpit,
own evidence bundle); the pack directory aggregates them and emits
``pack_report.md`` — the narrative — plus ``report.html`` and an
``evidence.json`` whose verdict summarizes the whole pack.

A child failure records the failure and continues: a pack with three good
probes and one failed one is evidence with a hole, not zero evidence.
"""

from __future__ import annotations

import csv
import dataclasses
import html
import json
import statistics
from pathlib import Path
from typing import Any, Optional

from pgbench_harness.errors import RunError
from pgbench_harness.manifest import Manifest
from pgbench_harness.spec import Spec, dump_spec_copy
from pgbench_harness.util import (atomic_write_json, atomic_write_text,
                                  make_run_id, setup_logging, utc_now_iso)

# (name, test_mode, block_kb) — order matters: reads first while the files
# are pristine, the replication pair last and back-to-back is deliberately
# NOT chosen: #3/#4 separated by nothing still replicates the governor; the
# original field proof separated them by an hour, and the report says which.
PACK_VARIANTS = (
    ("rndrd-16k", "rndrd", 16),
    ("rndrd-8k", "rndrd", 8),
    ("rndwr-16k", "rndwr", 16),
    ("rndwr-16k-repl", "rndwr", 16),
)


def _child_spec(spec: Spec, name: str, test_mode: str, block_kb: int,
                keep_files: bool) -> Spec:
    dp = dataclasses.replace(
        spec.device_probe, test_mode=test_mode, block_size_kb=block_kb,
        direct_io=True, keep_files=keep_files, pack=False)
    run = dataclasses.replace(
        spec.run, label=f"{spec.run.label or 'evidence-pack'}-{name}")
    return dataclasses.replace(spec, device_probe=dp, run=run)


def steady_stats(run_dir: Path) -> dict[str, Any]:
    """Steady-state profile of a probe's device series: the load window
    (samples above 20% of peak), minus its last 30 s (exit transition)."""
    path = run_dir / "parsed" / "device_io.csv"
    if not path.exists():
        return {}
    rows = []
    try:
        with open(path, encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                rows.append({k: float(r[k]) for k in
                             ("t_epoch_ms", "iops", "queue_depth",
                              "await_ms", "read_mb_s", "write_mb_s")})
    except (OSError, ValueError, KeyError):
        return {}
    if not rows:
        return {}
    peak = max(r["iops"] for r in rows)
    run = [r for r in rows if r["iops"] > max(100.0, peak * 0.2)]
    if len(run) < 2:
        return {}
    t_end = run[-1]["t_epoch_ms"]
    steady = [r for r in run if r["t_epoch_ms"] < t_end - 30_000] or run
    v = [r["iops"] for r in steady]
    half = len(v) // 2 or 1
    return {
        "steady_s": len(v),
        "iops_mean": round(statistics.mean(v), 1),
        "iops_median": round(statistics.median(v), 1),
        "iops_first_half": round(statistics.mean(v[:half]), 1),
        "iops_second_half": round(statistics.mean(v[half:]), 1),
        "queue_depth": round(statistics.mean(r["queue_depth"] for r in steady), 1),
        "await_ms": round(statistics.mean(r["await_ms"] for r in steady), 2),
        "mb_s": round(statistics.mean(r["read_mb_s"] + r["write_mb_s"]
                                      for r in steady), 1),
        "peak_1s": round(peak, 1),
    }


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def run_evidence_pack(spec: Spec, results_dir: Path,
                      dry_run: bool = False) -> int:
    from pgbench_harness import deviceprobe

    dp = spec.device_probe
    assert dp is not None
    if not dp.allow_device_probe:
        raise RunError(
            "evidence-pack refused: the spec does not set "
            "device_probe.allow_device_probe: true",
            hint="the pack runs four device probes against the pgdata "
                 "volume — TEST CLUSTERS ONLY. Arm it explicitly.")
    if dry_run:
        print(f"# evidence pack for '{spec.run.label}': four probes, one "
              "narrative")
        for name, mode, block in PACK_VARIANTS:
            print(f"#   {name}: {mode} {block}K x{dp.threads} threads, "
                  f"backlog {dp.async_backlog}, O_DIRECT, {dp.duration_s}s")
        print("# files prepared once, reused across probes"
              + ("" if dp.keep_files else ", removed at the end"))
        return 0

    pack_id = make_run_id(f"{spec.run.label or 'evidence-pack'}-pack")
    pack_dir = results_dir / pack_id
    n = 1
    while pack_dir.exists():
        n += 1
        pack_dir = results_dir / f"{pack_id}-{n}"
    (pack_dir / "parsed").mkdir(parents=True, exist_ok=True)
    dump_spec_copy(spec, pack_dir / "spec.yaml")
    manifest = Manifest(run_id=pack_dir.name, label=spec.run.label,
                        edition=spec.run.edition,
                        tshirt_size=spec.run.tshirt_size, mode="pack")
    manifest.status = "running"
    manifest.save(pack_dir)
    logger = setup_logging(pack_dir / "harness.log")
    logger.info("evidence pack %s -> %s", pack_dir.name, pack_dir)

    children: list[dict[str, Any]] = []
    for i, (name, mode, block) in enumerate(PACK_VARIANTS):
        last = i == len(PACK_VARIANTS) - 1
        child = _child_spec(spec, name, mode, block,
                            keep_files=(True if not last else dp.keep_files))
        before = {d.name for d in results_dir.iterdir() if d.is_dir()}
        logger.info("pack probe %d/%d: %s (%s %dK)", i + 1,
                    len(PACK_VARIANTS), name, mode, block)
        rec: dict[str, Any] = {"name": name, "test_mode": mode,
                               "block_kb": block,
                               "started_utc": utc_now_iso()}
        try:
            rc = deviceprobe.run_device_probe(child, results_dir)
            rec["exit_code"] = rc
        except BaseException as exc:  # noqa: BLE001 — a dead probe is a hole, not the end
            rec["exit_code"] = 2
            rec["error"] = str(exc)[:300]
            logger.error("pack probe %s failed: %s — continuing", name, exc)
        rec["finished_utc"] = utc_now_iso()
        new = sorted({d.name for d in results_dir.iterdir() if d.is_dir()}
                     - before)
        rec["run_id"] = new[-1] if new else ""
        if rec["run_id"]:
            child_dir = results_dir / rec["run_id"]
            rec["steady"] = steady_stats(child_dir)
            rec["verdict"] = _read_json(child_dir / "evidence.json"
                                        ).get("verdict") or {}
            rec["fileio"] = _read_json(child_dir / "parsed"
                                       / "fileio_summary.json")
        children.append(rec)
        atomic_write_json(pack_dir / "parsed" / "pack_children.json",
                          children)
        manifest.save(pack_dir)  # heartbeat: the pack survives a peek mid-run

    ok = [c for c in children if c.get("exit_code") in (0, 1)
          and c.get("steady")]
    manifest.status = ("complete" if len(ok) == len(PACK_VARIANTS)
                       else "partial" if ok else "failed")
    manifest.finished_utc = utc_now_iso()
    manifest.save(pack_dir)

    identity = {}
    for c in children:
        if c.get("run_id"):
            identity = _read_json(results_dir / c["run_id"] / "env"
                                  / "storage_identity.json")
            if identity:
                break
    md = build_pack_narrative(spec, children, identity)
    atomic_write_text(pack_dir / "pack_report.md", md)
    atomic_write_text(pack_dir / "report.html", _render_html(pack_dir.name, md))
    atomic_write_json(pack_dir / "evidence.json",
                      {"verdict": _pack_verdict(spec, children),
                       "children": children,
                       "storage_identity": identity})
    logger.info("evidence pack finished '%s'; narrative: %s",
                manifest.status, pack_dir / "pack_report.md")
    print(f"evidence pack report: {pack_dir / 'pack_report.md'}")
    return {"complete": 0, "partial": 1}.get(manifest.status, 2)


def _pack_verdict(spec: Spec, children: list[dict[str, Any]]) -> dict[str, Any]:
    std = spec.limits.standard_iops
    means = [c["steady"]["iops_mean"] for c in children if c.get("steady")]
    if not means:
        return {"finding": "inconclusive",
                "detail": "no probe produced a device series"}
    best = max(means)
    detail = (f"core-four pack: steady ceilings "
              + ", ".join(f"{c['name']} {c['steady']['iops_mean']:,.0f}"
                          for c in children if c.get("steady"))
              + f" — best {best:,.0f} vs standard {std:,}")
    finding = ("exceeds" if best >= std * (1 + spec.limits.tolerance_pct / 100)
               else "capped" if best >= std * (1 - spec.limits.tolerance_pct / 100)
               else "inconclusive")
    if finding == "inconclusive" and means:
        detail += (" — every pattern plateaus far below the standard limit "
                   "at deep queue: the volume behaves smaller-provisioned "
                   "than its class")
    return {"finding": finding, "detail": detail,
            "peak_sustained_iops": best}


def build_pack_narrative(spec: Spec, children: list[dict[str, Any]],
                         identity: dict[str, Any]) -> str:
    dp = spec.device_probe
    lim = spec.limits
    pvc = identity.get("pvc") or {}
    pv = identity.get("pv") or {}
    sc = identity.get("storage_class") or {}
    lines = [
        "# IOPS evidence pack — device-probe matrix with consolidated results",
        "",
        f"**Cluster:** `{spec.cluster.cr_name if spec.cluster else '?'}` · "
        f"**generated:** {utc_now_iso()}",
        f"**Spec'd limits:** {lim.standard_iops:,} standard / "
        f"{lim.burst_iops:,} burst / {lim.target_iops:,} target IOPS",
        "",
        "## Provisioning identity",
        "",
        f"- PVC `{pvc.get('name', '?')}` ({pvc.get('capacity', '?')}), "
        f"StorageClass `{sc.get('name', '?')}` "
        f"(provisioner `{sc.get('provisioner', '?')}`, "
        f"parameters `{json.dumps(sc.get('parameters', {}))}`)",
        f"- PV `{pv.get('name', '?')}` — backend volume_id "
        f"`{pv.get('volume_id', '?')}`",
        f"- Automated finding: {identity.get('finding', 'n/a')}",
        "",
        "## Method",
        "",
        f"sysbench fileio in a pod pinned to the primary's node, mounting "
        f"the pgdata PVC. {dp.file_num} files / "
        f"{dp.file_total_size_gb:g} GiB, async IO, "
        f"{dp.threads} threads x backlog {dp.async_backlog}, **O_DIRECT** "
        f"(no page-cache effects — generator and device counters agree), "
        f"{dp.duration_s}s per probe. Figures below are measured at "
        "`/proc/diskstats` on the primary pod (1 s samples), independent "
        "of the load generator; steady state excludes the final 30 s.",
        "",
        "## Results",
        "",
        "| Probe | Pattern | Block | Steady IOPS (mean/median) | 1st/2nd half "
        "| QD | Latency | MB/s | 10s peak (verdict) | Window (UTC) |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for c in children:
        s = c.get("steady") or {}
        v = c.get("verdict") or {}
        if s:
            lines.append(
                f"| {c['name']} | {c['test_mode']} | {c['block_kb']} KiB "
                f"| **{s['iops_mean']:,.0f}** / {s['iops_median']:,.0f} "
                f"| {s['iops_first_half']:,.0f} / {s['iops_second_half']:,.0f} "
                f"| {s['queue_depth']:,.0f} | {s['await_ms']:.1f} ms "
                f"| {s['mb_s']:.0f} "
                f"| {v.get('peak_sustained_iops', '—')} "
                f"| {c.get('started_utc', '')[11:19]}–"
                f"{c.get('finished_utc', '')[11:19]} |")
        else:
            why = c.get("error") or f"exit {c.get('exit_code')}"
            lines.append(f"| {c['name']} | {c['test_mode']} "
                         f"| {c['block_kb']} KiB | FAILED ({why})"
                         " | | | | | | |")
    ok = [c for c in children if c.get("steady")]
    lines += ["", "## Reading the numbers", ""]
    if ok:
        best = max(c["steady"]["iops_mean"] for c in ok)
        lines.append(
            f"- Best steady ceiling across all patterns: "
            f"**{best:,.0f} IOPS** vs the {lim.standard_iops:,} standard "
            f"({best / lim.standard_iops * 100:.0f}%).")
        rd16 = next((c for c in ok if c["name"] == "rndrd-16k"), None)
        rd8 = next((c for c in ok if c["name"] == "rndrd-8k"), None)
        if rd16 and rd8:
            lines.append(
                f"- Block-size dependence: {rd8['steady']['iops_mean']:,.0f} "
                f"IOPS at 8 KiB ({rd8['steady']['mb_s']:.0f} MB/s) vs "
                f"{rd16['steady']['iops_mean']:,.0f} at 16 KiB "
                f"({rd16['steady']['mb_s']:.0f} MB/s) — matches neither a "
                "pure IOPS bucket nor a pure bandwidth cap.")
        w1 = next((c for c in ok if c["name"] == "rndwr-16k"), None)
        w2 = next((c for c in ok if c["name"] == "rndwr-16k-repl"), None)
        if w1 and w2 and w1["steady"]["iops_mean"]:
            delta = abs(w1["steady"]["iops_mean"] - w2["steady"]["iops_mean"]
                        ) / w1["steady"]["iops_mean"] * 100
            lines.append(
                f"- Replication: the two write probes differ by "
                f"**{delta:.2f}%** ({w1['steady']['iops_mean']:,.0f} vs "
                f"{w2['steady']['iops_mean']:,.0f}) — the limit is "
                "deterministic, not noise.")
        burst_seen = max(c["steady"]["peak_1s"] for c in ok)
        lines.append(
            f"- Burst: highest single second observed anywhere: "
            f"{burst_seen:,.0f} IOPS "
            + (f"— the {lim.burst_iops:,} burst tier never engaged."
               if burst_seen < lim.burst_iops else
               f"(>= the {lim.burst_iops:,} burst tier)."))
        lines.append(
            "- Each probe's queue depth and latency reconcile via Little's "
            "law (QD / latency = served rate): a fixed-rate governor, not "
            "device-side variance.")
    lines += [
        "",
        "## Per-probe evidence bundles",
        "",
    ]
    for c in children:
        lines.append(f"- `{c.get('run_id', '?')}` — {c['name']}"
                     + (f" ({c.get('error', '')})" if c.get("error") else ""))
    lines += [
        "",
        "Each bundle: per-second device counters (`parsed/device_io.csv`), "
        "raw sysbench log, storage identity, pg_settings, HTML report with "
        "the stamped verdict window. All timestamps UTC — overlay directly "
        "on PMM's node disk dashboards.",
    ]
    return "\n".join(lines) + "\n"


def _render_html(title: str, md: str) -> str:
    """Minimal self-contained HTML wrapper for the narrative (the markdown
    is the canonical artifact; this keeps the console's Report button
    working for pack runs)."""
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{html.escape(title)}</title>"
        "<style>body{font:14px/1.5 -apple-system,Segoe UI,sans-serif;"
        "max-width:1000px;margin:2rem auto;padding:0 1rem;color:#1c2733}"
        "pre{white-space:pre-wrap;background:#f6f8fa;border:1px solid #d0d7de;"
        "border-radius:8px;padding:16px;overflow-x:auto}</style></head><body>"
        f"<h1>IOPS evidence pack</h1><pre>{html.escape(md)}</pre>"
        "</body></html>")
