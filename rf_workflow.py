"""High-level RF token-efficiency workflows for ADS Bridge 2.2.0.

Provides clean, modular implementations of:
1. Content-addressed netlist handle registry (`nl_<12hex>`) & parameter-only delta patching.
2. Atomic single-turn design evaluation macro (`design_eval_step`: patch -> ERC -> sim -> Scorecard).
3. Bridge-layer parallel local sensitivity ranking (`sensitivity_scan`).
4. Simulation log distillation (`job_log_summary`) for compact fault diagnostics.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import threading
import time
from typing import Any, Callable
import uuid

import netlist_parser
import rf_compress
import rf_optimizer
import worker as sim_worker


NETLIST_HANDLE_RE = re.compile(r"^nl_[0-9a-f]{12}$")
_SERVE_PREFIX = sim_worker.SERVE_JSON_PREFIX


# ---------------------------------------------------------------------------
# 0. Persistent ADS Python Worker Pool (Eliminates Per-Eval Cold Startup Tax)
# ---------------------------------------------------------------------------


class _SinglePersistentWorker:
    """Manages one persistent `worker.py serve` subprocess with JSON-lines framing."""

    def __init__(self, ads_root: Path, env: dict[str, str], cwd: Path) -> None:
        python = ads_root / "tools/python/python.exe"
        worker_py = Path(__file__).with_name("worker.py")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        self._proc = subprocess.Popen(
            [str(python), "-B", str(worker_py), "serve"],
            cwd=str(cwd),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=flags,
        )
        self._lock = threading.Lock()
        ready = self._read_response(timeout=45.0)
        if not ready.get("ready"):
            self.close()
            raise RuntimeError(f"Persistent ADS worker failed to initialize: {ready}")

    def _read_response(self, timeout: float) -> dict[str, Any]:
        assert self._proc.stdout is not None
        result_box: dict[str, Any] = {}
        err_box: list[BaseException] = []

        def _reader() -> None:
            try:
                while True:
                    line = self._proc.stdout.readline()  # type: ignore[union-attr]
                    if not line:
                        err_box.append(RuntimeError("Persistent ADS worker exited unexpectedly."))
                        return
                    if line.startswith(_SERVE_PREFIX):
                        result_box["payload"] = json.loads(line[len(_SERVE_PREFIX):])
                        return
            except BaseException as exc:
                err_box.append(exc)

        t = threading.Thread(target=_reader, daemon=True)
        t.start()
        t.join(timeout=timeout)
        if t.is_alive():
            self.close()
            raise TimeoutError(f"Persistent ADS worker timed out after {timeout}s.")
        if err_box:
            self.close()
            raise err_box[0]
        return result_box["payload"]

    def request(self, payload: dict[str, Any], timeout: float = 120.0) -> dict[str, Any]:
        with self._lock:
            if self._proc.poll() is not None or self._proc.stdin is None:
                raise RuntimeError("Persistent ADS worker is not running.")
            self._proc.stdin.write(json.dumps(payload, allow_nan=False) + "\n")
            self._proc.stdin.flush()
            return self._read_response(timeout=timeout)

    def close(self) -> None:
        if self._proc.poll() is None:
            try:
                if self._proc.stdin:
                    self._proc.stdin.write(json.dumps({"op": "quit"}) + "\n")
                    self._proc.stdin.flush()
            except Exception:
                pass
            try:
                self._proc.wait(timeout=2.0)
            except Exception:
                try:
                    subprocess.run(
                        ["taskkill", "/PID", str(self._proc.pid), "/T", "/F"],
                        capture_output=True,
                        timeout=5,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
                except Exception:
                    try:
                        self._proc.kill()
                    except Exception:
                        pass


class PersistentSimPool:
    """Context manager maintaining 1..N warm ADS Python workers for fast optimization & sensitivity loops."""

    def __init__(
        self,
        ads_root: Path,
        env: dict[str, str],
        runtime_root: Path,
        session_id: str,
        *,
        pool_size: int = 1,
        data_dir: str | Path | None = None,
        model_dirs: list[str | Path] | None = None,
        timeout_per_sim: float = 90.0,
    ) -> None:
        self.ads_root = ads_root
        self.env = env
        self.runtime_root = runtime_root
        self.session_id = session_id
        self.pool_size = max(1, min(4, int(pool_size)))
        self.context = {
            "data_dir": str(Path(data_dir).resolve()) if data_dir else None,
            "model_dirs": [str(Path(p).resolve()) for p in (model_dirs or [])],
        }
        self.timeout_per_sim = timeout_per_sim
        self._workers: list[_SinglePersistentWorker] = []
        self._avail: queue.Queue[_SinglePersistentWorker] = queue.Queue()
        self._extracted_cache: dict[str, dict[str, Any]] = {}
        self._dataset_cache: dict[str, Path] = {}
        self._cache_lock = threading.Lock()

    def __enter__(self) -> PersistentSimPool:
        cwd = self.runtime_root / "jobs" / self.session_id
        cwd.mkdir(parents=True, exist_ok=True)
        for _ in range(self.pool_size):
            w = _SinglePersistentWorker(self.ads_root, self.env, cwd)
            self._workers.append(w)
            self._avail.put(w)
        return self

    def __exit__(self, *_exc: Any) -> None:
        for w in self._workers:
            w.close()
        self._workers.clear()

    def simulate_and_extract(
        self,
        netlist_text: str,
        extract_specs: list[tuple[str | None, str | None]],
    ) -> dict[str, Any]:
        job_id = uuid.uuid4().hex
        job_dir = self.runtime_root / "jobs" / self.session_id / job_id
        extract_payload = [
            {"key": f"{b or ''}|{a or ''}", "block": b, "axis": a}
            for b, a in extract_specs
        ]
        req = {
            "op": "sim_extract",
            "job_id": job_id,
            "session_id": self.session_id,
            "job_dir": str(job_dir),
            "ads_root": str(self.ads_root),
            "request": {
                "source": "persistent_worker",
                "revision": hashlib.sha256(netlist_text.encode("utf-8")).hexdigest(),
                "block": None,
                "axis": None,
                "variable": None,
            },
            "context": self.context,
            "netlist": netlist_text,
            "extract": extract_payload,
            "write_catalog": False,
        }
        w = self._avail.get()
        try:
            resp = w.request(req, timeout=self.timeout_per_sim)
        except Exception as exc:
            # Respawn unhealthy worker cleanly
            try:
                w.close()
                w = _SinglePersistentWorker(self.ads_root, self.env, self.runtime_root / "jobs" / self.session_id)
            except Exception:
                pass
            return {"state": "failed", "code": "worker_error", "message": str(exc), "job_id": job_id}
        finally:
            self._avail.put(w)

        status = resp.get("status", {"state": "failed", "job_id": job_id})
        if status.get("state") == "succeeded":
            with self._cache_lock:
                self._extracted_cache[job_id] = resp.get("extracted", {})
                if status.get("dataset"):
                    self._dataset_cache[job_id] = Path(status["dataset"])
        return status

    def get_extracted_block(
        self,
        job_id: str,
        block: str | None,
        axis: str | None,
        filters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        flt_key = json.dumps(filters, sort_keys=True) if filters else ""
        ekey = f"{block or ''}|{axis or ''}" + (f"|{flt_key}" if flt_key else "")
        with self._cache_lock:
            cached = self._extracted_cache.get(job_id, {}).get(ekey)
            ds_path = self._dataset_cache.get(job_id)
        if cached is not None:
            return cached
        if ds_path is None:
            raise RuntimeError(f"Dataset for job {job_id} not found in persistent pool cache.")
        w = self._avail.get()
        try:
            resp = w.request(
                {"op": "extract", "dataset": str(ds_path), "block": block, "axis": axis, "filters": filters},
                timeout=45.0,
            )
        finally:
            self._avail.put(w)
        if not resp.get("ok"):
            raise RuntimeError(resp.get("error", "Persistent extract failed."))
        res = resp["result"]
        with self._cache_lock:
            self._extracted_cache.setdefault(job_id, {})[ekey] = res
        return res

    def get_extracted_sweep(
        self,
        job_id: str,
        sweep_axis: str,
        block: str | None = None,
        axis: str | None = None,
        filters: dict[str, Any] | None = None,
        signals: list[str] | None = None,
    ) -> dict[str, Any]:
        with self._cache_lock:
            ds_path = self._dataset_cache.get(job_id)
        if ds_path is None:
            raise RuntimeError(f"Dataset for job {job_id} not found in persistent pool cache.")
        w = self._avail.get()
        try:
            resp = w.request(
                {
                    "op": "extract_sweep",
                    "dataset": str(ds_path),
                    "sweep_axis": sweep_axis,
                    "block": block,
                    "axis": axis,
                    "signals": signals,
                    "filters": filters,
                },
                timeout=45.0,
            )
        finally:
            self._avail.put(w)
        if not resp.get("ok"):
            raise RuntimeError(resp.get("error", "Persistent sweep extract failed."))
        return resp["result"]


# ---------------------------------------------------------------------------
# 1. Content-Addressed Netlist Handle Store
# ---------------------------------------------------------------------------


def is_netlist_handle(value: Any) -> bool:
    """Return True if *value* is a valid bridge netlist handle (`nl_<12hex>`)."""
    return isinstance(value, str) and bool(NETLIST_HANDLE_RE.fullmatch(value.strip()))


def netlists_dir(runtime_root: Path, ensure_dir_fn: Callable[[Path], None]) -> Path:
    directory = runtime_root / "netlists"
    ensure_dir_fn(directory)
    return directory


def handle_path(runtime_root: Path, handle: str, ensure_dir_fn: Callable[[Path], None]) -> Path:
    cleaned = handle.strip()
    if not is_netlist_handle(cleaned):
        raise ValueError(f"Invalid netlist handle: {handle!r}")
    return netlists_dir(runtime_root, ensure_dir_fn) / f"{cleaned}.ckt"


def resolve_netlist_handle(
    runtime_root: Path,
    handle: str,
    ensure_dir_fn: Callable[[Path], None],
) -> tuple[str, Path]:
    """Load persisted netlist text for a `nl_<12hex>` handle."""
    path = handle_path(runtime_root, handle, ensure_dir_fn)
    if not path.is_file():
        raise FileNotFoundError(f"Netlist handle '{handle}' not found in bridge store.")
    return path.read_text(encoding="utf-8-sig"), path


def register_netlist(
    netlist_text: str,
    runtime_root: Path,
    ensure_dir_fn: Callable[[Path], None],
    *,
    compact_format: str = "tabular",
    include_models: bool = True,
    include_subcircuits: bool = True,
    search_dirs: list[str | Path] | None = None,
) -> dict[str, Any]:
    """Persist an ADS netlist under a deterministic `nl_<12hex>` handle and return compact JSON."""
    cleaned = netlist_text.lstrip("\ufeff")
    digest = hashlib.sha256(cleaned.encode("utf-8")).hexdigest()
    handle = f"nl_{digest[:12]}"
    dest = handle_path(runtime_root, handle, ensure_dir_fn)
    if not dest.is_file():
        dest.write_text(cleaned, encoding="utf-8")

    compact = netlist_parser.compress_netlist(
        cleaned,
        search_dirs=search_dirs,
        include_models=include_models,
        include_subcircuits=include_subcircuits,
        format=compact_format,
    )
    return {
        "schema": "ads_bridge.netlist_handle.v1",
        "netlist_id": handle,
        "revision": digest,
        "compact": compact,
    }


def patch_netlist(
    parent_text: str,
    updates: dict[str, str | float | int],
    runtime_root: Path,
    ensure_dir_fn: Callable[[Path], None],
    *,
    parent_id: str | None = None,
) -> dict[str, Any]:
    """Patch parameters on a netlist, store the updated handle, and return only the parameter delta."""
    patched = netlist_parser.apply_netlist_parameters(parent_text, updates)
    digest = hashlib.sha256(patched.encode("utf-8")).hexdigest()
    new_handle = f"nl_{digest[:12]}"
    dest = handle_path(runtime_root, new_handle, ensure_dir_fn)
    if not dest.is_file():
        dest.write_text(patched, encoding="utf-8")

    delta = netlist_parser.diff_netlist_params(parent_text, patched)
    out: dict[str, Any] = {
        "schema": "ads_bridge.netlist_patch.v1",
        "netlist_id": new_handle,
        "revision": digest,
        "delta": delta,
    }
    if parent_id:
        out["parent_id"] = parent_id
    return out


# ---------------------------------------------------------------------------
# 2. Simulation Log Distillation
# ---------------------------------------------------------------------------


def distill_job_log(job_dir: Path, max_items: int = 8) -> dict[str, Any]:
    """Distill a completed or failed simulation job's `sim.log` / `worker.log` into a compact fault card."""
    sim_log = job_dir / "sim.log"
    worker_log = job_dir / "worker.log"
    if sim_log.is_file():
        text = sim_log.read_text(encoding="utf-8", errors="replace")[-48000:]
        source = "sim.log"
    elif worker_log.is_file():
        text = worker_log.read_text(encoding="utf-8", errors="replace")[-48000:]
        source = "worker.log"
    else:
        text = ""
        source = "none"
    summary = sim_worker.distill_simulation_log(text, max_items=max_items)
    summary["source"] = source
    return summary


# ---------------------------------------------------------------------------
# 3. Single-Turn Atomic Macro: design_eval_step
# ---------------------------------------------------------------------------


def run_design_eval_step(
    base_text: str,
    runtime_root: Path,
    ensure_dir_fn: Callable[[Path], None],
    *,
    simulate_fn: Callable[..., dict[str, Any]],
    extract_block_fn: Callable[..., dict[str, Any]],
    extract_sweep_fn: Callable[..., dict[str, Any]] | None = None,
    updates: dict[str, str | float | int] | None = None,
    sweep: dict[str, Any] | None = None,
    specs: list[dict[str, Any]] | None = None,
    erc_mode: str | None = "tb",
    compress_block: str | None = None,
    compress_axis: str | None = None,
    signals: list[str] | None = None,
    filters: dict[str, Any] | None = None,
    max_curve_points: int = 16,
    band_hz: list[float] | None = None,
    data_dir: str | Path | None = None,
    model_dirs: list[str | Path] | None = None,
    timeout: float = 180.0,
) -> dict[str, Any]:
    """Execute Upfront Spec Validation -> Patch -> Static ERC -> 64-bit ADS Simulation -> Spec Scorecard."""
    # Step 0: Apply step-level default filters to specs and validate BEFORE running ERC or launching hpeesofsim
    effective_specs: list[dict[str, Any]] | None = None
    if specs:
        effective_specs = [
            ({**sp, "filters": filters} if (filters and isinstance(sp, dict) and not sp.get("filters")) else sp)
            for sp in specs
        ]
        rf_optimizer._validate_objectives(effective_specs)

    # Step 1: Optional parameter patch + deterministic handle registration
    delta: dict[str, Any] | None = None
    active_text = base_text.lstrip("\ufeff")
    if updates:
        active_text = netlist_parser.apply_netlist_parameters(active_text, updates)
        delta = netlist_parser.diff_netlist_params(base_text, active_text)

    digest = hashlib.sha256(active_text.encode("utf-8")).hexdigest()
    handle = f"nl_{digest[:12]}"
    dest = handle_path(runtime_root, handle, ensure_dir_fn)
    if not dest.is_file():
        dest.write_text(active_text, encoding="utf-8")

    # Step 2: Fast static ERC guard before launching hpeesofsim
    if erc_mode is not None:
        erc = netlist_parser.check_netlist(active_text, mode=erc_mode, search_dirs=model_dirs)
        if not erc["ok"]:
            diags = erc.get("diagnostics", ())
            erc_errors = [d for d in diags if d.get("severity") == "error"]
            erc_warnings = [d for d in diags if d.get("severity") == "warning"]
            return {
                "schema": "ads_bridge.eval_step.v1",
                "ok": False,
                "stage": "erc",
                "netlist_id": handle,
                "delta": delta,
                "erc_errors": erc_errors[:10],
                "erc_warnings": erc_warnings[:5],
            }

    # Step 3: Run 64-bit ADS simulation
    sim = simulate_fn(
        active_text,
        data_dir=data_dir,
        model_dirs=model_dirs,
        sweep=sweep,
        wait=True,
        timeout=timeout,
    )
    if sim.get("state") != "succeeded":
        return {
            "schema": "ads_bridge.eval_step.v1",
            "ok": False,
            "stage": "simulation",
            "netlist_id": handle,
            "job_id": sim.get("job_id"),
            "delta": delta,
            "code": sim.get("code"),
            "message": sim.get("message"),
            "log_summary": sim.get("log_summary"),
        }

    job_id = sim["job_id"]
    out: dict[str, Any] = {
        "schema": "ads_bridge.eval_step.v1",
        "ok": True,
        "stage": "completed",
        "netlist_id": handle,
        "job_id": job_id,
        "session_id": sim.get("session_id"),
        "dataset": sim.get("dataset"),
    }
    if delta is not None:
        out["delta"] = delta

    def _extract_blk(blk: str | None, ax: str | None, flt: dict[str, Any] | None = None) -> dict[str, Any]:
        if flt:
            try:
                return extract_block_fn(job_id, blk, ax, flt)
            except TypeError:
                pass
        return extract_block_fn(job_id, blk, ax)

    # Step 4: Evaluate declarative RF/circuit spec scorecard (evaluates every sweep slice if 2D and unfiltered)
    if effective_specs:
        sw_cb = (
            (lambda sw_ax, blk, ax, flt=None: extract_sweep_fn(job_id, sw_ax, blk, ax, flt))
            if extract_sweep_fn is not None
            else None
        )
        score = rf_optimizer.evaluate_specs_scorecard(
            effective_specs,
            _extract_blk,
            extract_sweep_fn=sw_cb,
        )
        out["all_pass"] = score["all_pass"]
        out["total_cost"] = score["total_cost"]
        out["scorecard_cols"] = score["scorecard_cols"]
        out["scorecard"] = score["scorecard"]
        if "sweep_evaluation" in score:
            out["sweep_evaluation"] = score["sweep_evaluation"]

    # Step 5: Optional compact waveform / S-parameter summary
    if compress_block is not None or not effective_specs:
        raw = _extract_blk(compress_block, compress_axis, filters)
        outer_axes = raw.get("outer_axes") or {}
        if outer_axes and len(outer_axes) == 1 and extract_sweep_fn is not None:
            sw_axis = next(iter(outer_axes.keys()))
            raw_sw = extract_sweep_fn(job_id, sw_axis, raw["block"], raw["axis"], filters, signals)
            out["compressed"] = rf_compress.compress_sweep_slices(
                raw_sw["axis"],
                raw_sw["sweep_axis"],
                raw_sw["slices"],
                block_name=raw_sw["block"],
                max_curve_points=max_curve_points,
                band_hz=band_hz,
            )
        else:
            selected_sigs = raw["signals"]
            if signals:
                missing = [s for s in signals if s not in selected_sigs]
                if missing:
                    raise ValueError(f"Requested signals {missing} not in block '{raw['block']}'. Available: {sorted(selected_sigs)}")
                selected_sigs = {s: selected_sigs[s] for s in signals}
            sig_complex = {
                name: rf_compress.decode_complex_pairs(pairs)
                for name, pairs in selected_sigs.items()
            }
            out["compressed"] = rf_compress.compress_waveform_block(
                raw["axis"],
                raw["axis_values"],
                sig_complex,
                block_name=raw["block"],
                max_curve_points=max_curve_points,
                band_hz=band_hz,
                outer_axes=outer_axes or None,
            )

    log_summary = sim.get("log_summary")
    if isinstance(log_summary, dict) and log_summary.get("warning_count", 0) > 0:
        out["sim_warnings"] = log_summary.get("unique_warnings", [])[:5]
    return out
