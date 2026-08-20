"""Continuous mode: an always-on fixed-concurrency workload that runs until a
user explicitly stops it.

Durability model (three failure classes, three owners):

* sysbench crash / connection loss  -> THIS supervisor relaunches it with a
  capped exponential backoff + full jitter (auth failures jump straight to the
  max interval so a bad credential never hammers the target).
* worker/web restart                -> the harness child survives
  (``KillMode=process``) and the worker re-attaches; nothing here changes.
* droplet reboot / harness crash    -> the web tier's ``desired_state``
  reconcile relaunches this CLI with ``--run-dir <same run>``, which appends a
  NEW segment to the same run directory. The run directory is the durable
  identity; this module only ever appends to it.

Timeline model: identical to soak — every raw line is stamped with read-time
UTC (``<ISO-UTC>\\t<sysbench line>``); absent seconds ARE the outage signal.
Unlike soak there is no horizon: segments are bounded (``segment_time_s``)
purely so raw logs stay prunable, and a clean boundary exit relaunches
immediately without counting as a failure.

The filesystem stays the source of truth: raw ``raw/cont_seg<NNNN>.log``
segments are canonical; ``parsed/cont_timeseries.csv`` is an incremental
convenience view; the web tier's SQLite tables are a rebuildable index
(``pgbench-web reindex-continuous``).
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from pgbench_harness import capture, sysbench
from pgbench_harness.errors import PreflightError, RunError
from pgbench_harness.manifest import Manifest
from pgbench_harness.spec import Spec, dump_spec_copy, load_spec
from pgbench_harness.summarize import IncrementalCsvWriter
from pgbench_harness.util import (
    atomic_write_json, fmt_duration, get_redactor, make_run_id, setup_logging,
)

# Columns of parsed/cont_timeseries.csv (same philosophy as soak's
# TIMESERIES_COLUMNS, keyed on absolute UTC instead of a run offset, because a
# continuous run has no finite horizon and survives reboots).
CONT_TIMESERIES_COLUMNS = ["ts_utc", "tps", "qps", "qps_r", "qps_w", "qps_o",
                           "lat_p99", "err_s", "reconn_s", "threads", "seg"]

SEGMENT_GLOB = "cont_seg*.log"
SEGMENT_FMT = "cont_seg{n:04d}.log"
STATE_FILE = "state.json"
HEARTBEAT_S = 10.0
# manifest segment-list cap: the manifest is rewritten per segment, so an
# unbounded list would make every rewrite slower forever. The raw/ dir and
# events.jsonl remain the full record.
MANIFEST_SEGMENT_CAP = 500

AUTH_ERROR_RE = re.compile(
    r"password authentication failed|no pg_hba\.conf entry|"
    r"role .* does not exist|authentication failed", re.IGNORECASE)
NETWORK_ERROR_RE = re.compile(
    r"connection refused|could not connect|timeout|timed out|"
    r"server closed the connection|connection reset|no route to host|"
    r"could not translate host name|connection to server .* failed|"
    r"network is unreachable", re.IGNORECASE)


def classify_error(excerpt: str) -> str:
    """Classify a dead segment's error text: 'auth' | 'network' | 'other'.

    Auth wins over network: "password authentication failed" arriving over a
    perfectly healthy connection must never be retried on the fast ladder.
    """
    if AUTH_ERROR_RE.search(excerpt):
        return "auth"
    if NETWORK_ERROR_RE.search(excerpt):
        return "network"
    return "other"


def next_segment_number(run_dir: Path) -> int:
    """1 + the highest existing segment number (resume/reboot appends)."""
    best = 0
    for p in (run_dir / "raw").glob(SEGMENT_GLOB):
        m = re.search(r"cont_seg(\d+)\.log$", p.name)
        if m:
            best = max(best, int(m.group(1)))
    return best + 1


def read_state(run_dir: Path) -> dict[str, Any]:
    """Best-effort read of the supervisor heartbeat (state.json)."""
    try:
        doc = json.loads((run_dir / STATE_FILE).read_text(encoding="utf-8"))
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def _iso_micros() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class _StateWriter:
    """Atomic state.json heartbeat: immediate writes on transitions plus a
    background tick every ~10s so 'is the supervisor alive' is answerable
    from the filesystem alone (the web tier's no_data alert keys off this)."""

    def __init__(self, run_dir: Path, run_id: str) -> None:
        self._path = run_dir / STATE_FILE
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._doc: dict[str, Any] = {
            "run_id": run_id,
            "supervisor_pid": os.getpid(),
            "status": "starting",
            "seg": 0,
            "segments_total": 0,
            "relaunches": 0,
            "consecutive_failures": 0,
            "last_sample_utc": "",
            "last_error_class": "",
            "last_error": "",
            "last_backoff_s": 0.0,
            "next_retry_utc": "",
            "load_stopped": False,
        }
        self._thread: Optional[threading.Thread] = None

    def update(self, **fields: Any) -> None:
        with self._lock:
            self._doc.update(fields)
        self._flush()

    def touch_sample(self, ts_utc: str) -> None:
        """Record the latest sample time WITHOUT flushing (called per line —
        the heartbeat tick persists it)."""
        with self._lock:
            self._doc["last_sample_utc"] = ts_utc

    def _flush(self) -> None:
        with self._lock:
            doc = dict(self._doc)
        doc["updated_utc"] = _iso_micros()
        try:
            atomic_write_json(self._path, doc)
        except OSError:
            pass  # a heartbeat write failure must never kill the load

    def start(self) -> None:
        self._flush()
        self._thread = threading.Thread(target=self._run, name="cont-state",
                                        daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(HEARTBEAT_S):
            self._flush()

    def stop(self, status: str) -> None:
        self._stop.set()
        self.update(status=status)
        if self._thread is not None:
            self._thread.join(timeout=2)


def _segment_error_excerpt(seg_log: Path, rc: int, n_intervals: int) -> str:
    """Reuse the soak error-excerpt logic (timestamp-prefixed raw lines)."""
    from pgbench_harness.runner import _segment_error_excerpt as _soak_excerpt
    return _soak_excerpt(seg_log, rc, n_intervals)


def _live_cont_callback(
    live: IncrementalCsvWriter, seg_name: str, state: _StateWriter,
    last_second: dict[str, str],
) -> Callable[[str, str], None]:
    """Per-line tap: parse each interval and append it to the live CSV keyed on
    read-time UTC. Consecutive same-second duplicates (segment-boundary
    overlap) are skipped; the web tier's PRIMARY KEY is the hard dedup."""
    from pgbench_harness.parser import parse_interval_line

    def _cb(ts: str, line: str) -> None:
        s = parse_interval_line(line)
        if s is None:
            return
        second = ts[:19]                      # ISO second precision
        if second == last_second.get("s"):
            return
        last_second["s"] = second
        live.append([ts, s.tps, s.qps, s.r, s.w, s.o, s.lat_ms,
                     s.err_s, s.reconn_s, s.threads, seg_name])
        state.touch_sample(ts)
    return _cb


def _interruptible_sleep(seconds: float, stop: dict) -> None:
    """Sleep up to *seconds*, returning promptly when a stop is requested —
    a cancel during the backoff between relaunches must not wait it out."""
    deadline = time.monotonic() + seconds
    while not stop.get("flag") and time.monotonic() < deadline:
        time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))


def _backoff_s(consecutive_failures: int, ladder: tuple[int, ...],
               error_class: str, rng: random.Random) -> float:
    """Capped exponential backoff with FULL jitter (uniform over [0, base]).

    Auth failures jump straight to the max interval: retrying a bad password
    on the fast ladder just hammers the cluster's auth path and can trip
    connection-attempt limiting on managed PG.
    """
    if error_class == "auth":
        base = ladder[-1]
    else:
        base = ladder[min(max(consecutive_failures, 1) - 1, len(ladder) - 1)]
    return max(0.1, rng.uniform(0, float(base)))


def _cont_doc(manifest: Manifest, start_utc: str, segments: list[dict[str, Any]],
              segments_total: int, relaunches: int, threads: int) -> dict[str, Any]:
    return {"run_id": manifest.run_id, "start_utc": start_utc,
            "segments": segments[-MANIFEST_SEGMENT_CAP:],
            "segments_total": segments_total,
            "relaunches": relaunches, "threads": threads}


def _continuous_supervisor(
    spec: Spec, password: str, run_dir: Path, manifest: Manifest,
    logger: logging.Logger, stop: dict,
) -> str:
    """Run sysbench at fixed concurrency FOREVER, relaunching on every exit.

    Returns the terminal status ('stopped' on a graceful stop). Raises
    RunError only for local fatal conditions (results volume full).
    """
    cont = spec.continuous
    assert cont is not None
    from pgbench_harness.runner import _append_event, _disk_guard
    env = sysbench.child_env(spec, password)
    rng = random.Random()

    prev = manifest.continuous or {}
    start_utc = prev.get("start_utc") or _iso_micros()
    segments: list[dict[str, Any]] = list(prev.get("segments") or [])
    segments_total = int(prev.get("segments_total") or len(segments))
    relaunches = int(prev.get("relaunches") or 0)
    consecutive_failures = 0
    consecutive_instant = 0
    load_stopped = False
    disk_warned: dict = {}
    last_second: dict[str, str] = {}

    state = _StateWriter(run_dir, manifest.run_id)
    state.start()
    manifest.continuous = _cont_doc(manifest, start_utc, segments,
                                    segments_total, relaunches, cont.threads)
    manifest.save(run_dir)

    live = IncrementalCsvWriter(run_dir / "parsed" / "cont_timeseries.csv",
                                CONT_TIMESERIES_COLUMNS)
    try:
        while not stop.get("flag"):
            if load_stopped:
                # gave up launching (max_consecutive_failures reached) but the
                # supervisor stays alive: the web tier keeps probing/alerting
                # and a Stop still lands gracefully.
                _interruptible_sleep(1.0, stop)
                continue
            seg_no = next_segment_number(run_dir)
            _disk_guard(run_dir, logger, disk_warned)
            seg_log = run_dir / "raw" / SEGMENT_FMT.format(n=seg_no)
            seg_name = seg_log.stem
            cmd = sysbench.build_continuous_command(spec, cont.threads,
                                                    cont.segment_time_s)
            state.update(status="running", seg=seg_no)
            logger.info("continuous segment %d: %s", seg_no, cmd.display())
            seg_start = _iso_micros()
            seg_mono = time.monotonic()

            inner_cb = _live_cont_callback(live, seg_name, state, last_second)
            disk_state: dict[str, Any] = {"next_check": time.monotonic() + 60.0}

            def _cb(ts_iso: str, red_line: str,
                    _inner: Callable[[str, str], None] = inner_cb,
                    _ds: dict = disk_state) -> None:
                _inner(ts_iso, red_line)
                if stop.get("flag"):
                    # a stop that raced the segment launch (flag set between
                    # the loop check and Popen) still lands within ~1 line
                    p = stop["procs"].get("proc")
                    if p is not None:
                        try:
                            p.terminate()
                        except OSError:
                            pass
                if time.monotonic() >= _ds["next_check"]:
                    _ds["next_check"] = time.monotonic() + 60.0
                    try:
                        _disk_guard(run_dir, logger, disk_warned)
                    except RunError as exc:
                        _ds["abort"] = str(exc)
                        p = stop["procs"].get("proc")
                        if p is not None:
                            try:
                                p.terminate()
                            except OSError:
                                pass

            rc, n_intervals, timed_out = sysbench.run_streaming_timestamped(
                cmd, env, seg_log, logger,
                timeout_s=float(cont.segment_time_s + cont.segment_kill_grace_s),
                kill_grace_s=float(cont.segment_kill_grace_s),
                on_line=_cb, proc_holder=stop["procs"])
            seg_wall = time.monotonic() - seg_mono

            # A clean boundary exit that produced (approximately) a full
            # window of samples is a planned log rotation, not a failure.
            planned = (rc == 0 and not timed_out
                       and n_intervals >= max(1, int(cont.segment_time_s * 0.9)))
            excerpt = ""
            error_class = ""
            if not planned and not stop.get("flag"):
                excerpt = _segment_error_excerpt(seg_log, rc, n_intervals)
                error_class = classify_error(excerpt)
                head = excerpt.splitlines()[0] if excerpt else ""
                logger.error("continuous segment %d exited (rc %d, %d intervals, "
                             "class %s%s): %s", seg_no, rc, n_intervals,
                             error_class, ", timed out" if timed_out else "", head)
            segments.append({"seg": seg_no, "log": f"raw/{seg_log.name}",
                             "started_utc": seg_start, "finished_utc": _iso_micros(),
                             "exit_code": rc, "intervals": n_intervals,
                             "timed_out": timed_out, "wall_s": round(seg_wall, 1),
                             "planned": planned, "error_class": error_class,
                             "error_excerpt": excerpt})
            segments_total += 1

            if stop.get("flag"):
                break
            if planned:
                consecutive_failures = 0
                consecutive_instant = 0
                state.update(segments_total=segments_total,
                             consecutive_failures=0,
                             last_error_class="", last_error="")
                manifest.continuous = _cont_doc(manifest, start_utc, segments,
                                                segments_total, relaunches,
                                                cont.threads)
                manifest.save(run_dir)
                if disk_state.get("abort"):
                    raise RunError(disk_state["abort"])
                continue

            # unplanned exit: a relaunch with backoff
            relaunches += 1
            # a healthy long-lived segment that finally crashed restarts the
            # ladder; instant churn climbs it
            consecutive_failures = (1 if n_intervals >= 30
                                    else consecutive_failures + 1)
            consecutive_instant = (consecutive_instant + 1 if n_intervals == 0
                                   else 0)
            _append_event(run_dir, "loadgen_restart",
                          f"sysbench relaunch #{relaunches}",
                          f"supervisor relaunching after early exit "
                          f"(class {error_class or 'unknown'})", "auto")
            manifest.continuous = _cont_doc(manifest, start_utc, segments,
                                            segments_total, relaunches,
                                            cont.threads)
            manifest.save(run_dir)
            if disk_state.get("abort"):
                raise RunError(disk_state["abort"])

            if (cont.max_consecutive_failures
                    and consecutive_instant >= cont.max_consecutive_failures):
                load_stopped = True
                logger.error(
                    "continuous: %d consecutive instant-exit relaunches — "
                    "stopping the LOAD (probing/alerting continues; stop and "
                    "resume the workload to retry). Last error: %s",
                    consecutive_instant,
                    (excerpt.splitlines()[0] if excerpt else f"exit {rc}"))
                _append_event(run_dir, "note", "load generator gave up",
                              f"{consecutive_instant} consecutive instant exits "
                              f"(max_consecutive_failures="
                              f"{cont.max_consecutive_failures})", "auto")
                state.update(status="load_stopped", load_stopped=True,
                             segments_total=segments_total,
                             relaunches=relaunches,
                             consecutive_failures=consecutive_failures,
                             last_error_class=error_class,
                             last_error=(excerpt.splitlines()[0] if excerpt
                                         else f"exit {rc}"))
                continue

            delay = _backoff_s(consecutive_failures, cont.restart_backoff_s,
                               error_class, rng)
            retry_at = datetime.now(timezone.utc).timestamp() + delay
            state.update(status="backoff",
                         segments_total=segments_total,
                         relaunches=relaunches,
                         consecutive_failures=consecutive_failures,
                         last_error_class=error_class,
                         last_error=(excerpt.splitlines()[0] if excerpt else
                                     f"exit {rc}"),
                         last_backoff_s=round(delay, 2),
                         next_retry_utc=datetime.fromtimestamp(
                             retry_at, timezone.utc).strftime(
                             "%Y-%m-%dT%H:%M:%SZ"))
            logger.warning("continuous: relaunching in %.1fs (failure #%d, "
                           "class %s)", delay, consecutive_failures,
                           error_class or "unknown")
            _interruptible_sleep(delay, stop)
    finally:
        live.close()
        manifest.continuous = _cont_doc(manifest, start_utc, segments,
                                        segments_total, relaunches, cont.threads)
        try:
            manifest.save(run_dir)      # the final segment record must persist
        except OSError:
            pass
        state.stop("stopped")

    logger.info("continuous: stop requested — finalized after %d segment(s), "
                "%d relaunch(es)", segments_total, relaunches)
    return "stopped"


def print_continuous_dry_run(spec: Spec) -> None:
    """Print the segment command, backoff ladder, and durability contract."""
    assert spec.continuous is not None
    c = spec.continuous
    cmd = sysbench.build_continuous_command(spec, c.threads, c.segment_time_s)
    print(f"# continuous dry run for '{spec.run.label}' — runs until stopped")
    print(cmd.display())
    print(f"# fixed concurrency {c.threads}; segments rotate every "
          f"{fmt_duration(c.segment_time_s)} (planned, not a failure)")
    print(f"# relaunch backoff ladder: {list(c.restart_backoff_s)}s "
          "(capped, full jitter; auth errors jump to the max)")
    give_up = (f"stop the load after {c.max_consecutive_failures} instant-exit "
               "relaunches" if c.max_consecutive_failures else "never give up")
    print(f"# max_consecutive_failures: {give_up}")
    print(f"# password source: env var {spec.target.password_env} -> PGPASSWORD")


def cmd_continuous(spec_path: Path, results_dir: Path,
                   run_dir_opt: Optional[Path] = None,
                   dry_run: bool = False) -> int:
    """`continuous` subcommand: always-on load until explicitly stopped.

    ``--run-dir`` pointing at an existing continuous run appends a new segment
    to that run — the reboot/relaunch resume path. Exit 0 = graceful stop.
    """
    spec = load_spec(spec_path)
    if not spec.is_continuous:
        raise RunError("this spec has no 'continuous' section",
                       hint="add a continuous: block (threads) for always-on runs.")
    assert spec.continuous is not None
    if dry_run:
        print_continuous_dry_run(spec)
        return 0
    password = spec.password()
    get_redactor().register(password)

    resume = False
    if run_dir_opt is not None:
        run_dir = run_dir_opt
        if (run_dir / "manifest.json").exists():
            manifest = Manifest.load(run_dir)
            if manifest.mode != "continuous":
                raise RunError(
                    f"--run-dir {run_dir} is a '{manifest.mode}' run, not continuous",
                    hint="pass the run directory of an existing continuous run.")
            resume = True
        elif run_dir.exists() and any(run_dir.iterdir()):
            raise RunError(f"--run-dir {run_dir} exists but has no manifest.json",
                           hint="pass an existing continuous run directory, or "
                                "omit --run-dir to start a fresh run.")
        else:
            manifest = Manifest(run_id=run_dir.name, label=spec.run.label,
                                edition=spec.run.edition,
                                tshirt_size=spec.run.tshirt_size,
                                mode="continuous")
    else:
        run_id = make_run_id(spec.run.label)
        run_dir = results_dir / run_id
        n = 1
        while run_dir.exists():
            n += 1
            run_id = f"{make_run_id(spec.run.label)}-{n}"
            run_dir = results_dir / run_id
        manifest = Manifest(run_id=run_id, label=spec.run.label,
                            edition=spec.run.edition,
                            tshirt_size=spec.run.tshirt_size, mode="continuous")
    (run_dir / "raw").mkdir(parents=True, exist_ok=True)
    if not resume:
        dump_spec_copy(spec, run_dir / "spec.yaml")
        dump_spec_copy(spec, run_dir / "env" / "spec.yaml")
    manifest.status = "running"
    manifest.finished_utc = ""
    manifest.save(run_dir)
    logger = setup_logging(run_dir / "harness.log")
    logger.info("continuous %s -> %s (%d threads, until stopped%s)",
                manifest.run_id, run_dir, spec.continuous.threads,
                ", resumed" if resume else "")

    # Fresh runs preflight hard (a misconfigured submission must fail fast);
    # a RESUME must NOT: the target being down at reboot/relaunch time is
    # exactly the outage the backoff ladder + prober exist to ride through.
    try:
        pf = capture.run_preflight(spec, password, logger)
        assert pf.dataset is not None
        if not pf.dataset.ok:
            from pgbench_harness.runner import _dataset_error
            raise _dataset_error(pf.dataset, spec_path)
        manifest.preflight = _preflight_doc(pf)
        manifest.save(run_dir)
        if not resume:
            capture.capture_env(run_dir, spec, password, pf)
    except PreflightError:
        if not resume:
            manifest.status = "failed"
            manifest.save(run_dir)
            raise
        logger.warning("continuous resume: preflight failed (target may be "
                       "down) — starting the supervisor anyway; the backoff "
                       "ladder owns the retry")

    # NOTE: no LivePgSampler here — the web tier's DB-side collector owns
    # engine metrics for continuous runs (pg_timeseries.csv would otherwise
    # grow without bound for the lifetime of the workload).

    import signal
    stop: dict = {"flag": False, "procs": {}}

    def _on_signal(_signum: int, _frame: object) -> None:
        stop["flag"] = True
        p = stop["procs"].get("proc")
        if p is not None:
            try:
                p.terminate()
            except OSError:
                pass

    old_int = signal.signal(signal.SIGINT, _on_signal)
    old_term = signal.signal(signal.SIGTERM, _on_signal)
    supervisor_error: Optional[BaseException] = None
    status = "failed"
    try:
        status = _continuous_supervisor(spec, password, run_dir, manifest,
                                        logger, stop)
    except Exception as exc:  # noqa: BLE001 — finalize a terminal manifest, then re-raise
        supervisor_error = exc
        logger.error("continuous aborted: %s", exc)
    finally:
        signal.signal(signal.SIGINT, old_int)
        signal.signal(signal.SIGTERM, old_term)
        manifest.status = status if supervisor_error is None else "failed"
        manifest.finished_utc = _iso_micros()[:19] + "Z"
        manifest.save(run_dir)
    if supervisor_error is not None:
        raise supervisor_error
    return 0


def _preflight_doc(pf: capture.PreflightResult) -> dict[str, Any]:
    import dataclasses
    return dataclasses.asdict(pf)
