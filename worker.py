"""Vendor Python worker for circuit simulations, dataset analytics, and headless design flows."""

from __future__ import annotations

import json
import math
import msvcrt
import os
from pathlib import Path
import re
import sys
import time
from typing import Any

from bridge import BridgeError, read_json, write_json


_LOG_NOISE_PATTERNS = (
    "keysight technologies",
    "hpeesofsim (*)",
    "copyright",
    "all rights reserved",
    "simulation / synthesis",
    "number of threads",
    "resource usage",
    "total stopwatch time",
)


def distill_simulation_log(log_text: str, max_items: int = 8) -> dict[str, Any]:
    """Distill verbose hpeesofsim stdout/stderr logs into a compact (<300-token) fault/warning card.

    Preserves indented detail lines immediately following ADS `Error/Warning detected by hpeesofsim`
    headers (e.g. `No convergence.`, `Undefined variable ...`, `Number of nodes with no DC path ...`).
    """
    err_counts: dict[str, int] = {}
    warn_counts: dict[str, int] = {}
    excerpt: list[str] = []
    raw_lines = log_text.splitlines()
    total_lines = len(raw_lines)

    idx = 0
    while idx < total_lines:
        raw = raw_lines[idx]
        idx += 1
        s = raw.strip()
        if not s:
            continue
        low = s.lower()
        if any(pat in low for pat in _LOG_NOISE_PATTERNS):
            continue

        is_err = "error detected by hpeesofsim" in low or low.startswith("error:") or "fatal" in low or "syntax error" in low
        is_warn = (not is_err) and ("warning detected by hpeesofsim" in low or low.startswith("warning:"))
        if not (is_err or is_warn):
            continue

        # Collect up to 2 indented continuation/detail lines belonging to this ADS diagnostic block
        detail_parts = [re.sub(r"\s+", " ", s)]
        collected = 0
        while idx < total_lines and collected < 2:
            nxt = raw_lines[idx]
            nxt_s = nxt.strip()
            if not nxt_s:
                break
            nxt_low = nxt_s.lower()
            if nxt.startswith((" ", "\t")) and not (
                "error detected by hpeesofsim" in nxt_low
                or "warning detected by hpeesofsim" in nxt_low
                or nxt_low.startswith(("error:", "warning:"))
            ):
                detail_parts.append(re.sub(r"\s+", " ", nxt_s))
                idx += 1
                collected += 1
            else:
                break
        norm = " | ".join(detail_parts)[:320]
        if is_err:
            err_counts[norm] = err_counts.get(norm, 0) + 1
            if len(excerpt) < max_items:
                excerpt.append(norm)
        else:
            warn_counts[norm] = warn_counts.get(norm, 0) + 1

    haystack = log_text.lower()
    hints: list[str] = []
    if "singular" in haystack or "floating" in haystack or "dc path to ground" in haystack:
        hints.append("Check for floating DC node (add high-value bleed resistor to ground or verify ground net '0').")
    if "undefined" in haystack or "unresolved" in haystack:
        hints.append("Verify VAR/parameter names and subcircuit parameter bindings via netlist_check.")
    if "cannot open file" in haystack or "unable to find model" in haystack or "undefined model" in haystack:
        hints.append("Pass missing PDK/model paths via model_dirs or check #include file path.")
    if "convergence" in haystack or "failed to converge" in haystack or "gmin" in haystack:
        hints.append("Try increasing Oversample/MaxIter, relaxing initial bias ramp, or checking nonlinear model bounds.")
    if "license" in haystack and ("failed" in haystack or "unavailable" in haystack or "no such feature" in haystack):
        hints.append("Verify Keysight ADS license server connectivity and active feature checkout.")

    unique_errors = [{"msg": msg, "count": cnt} for msg, cnt in list(err_counts.items())[:max_items]]
    unique_warnings = [{"msg": msg, "count": cnt} for msg, cnt in list(warn_counts.items())[:max_items]]
    return {
        "total_log_lines": total_lines,
        "error_count": sum(err_counts.values()),
        "warning_count": sum(warn_counts.values()),
        "unique_errors": unique_errors,
        "unique_warnings": unique_warnings,
        "hints": hints,
        "excerpt": excerpt,
    }


def _classify_sim_failure(exc: Exception, sim_log_path: Path) -> tuple[str, str, list[str], list[str], dict[str, Any]]:
    tail = ""
    if sim_log_path.is_file():
        try:
            tail = sim_log_path.read_text(encoding="utf-8", errors="replace")[-32000:]
        except OSError:
            tail = ""
    summary = distill_simulation_log(tail)
    errors = [item["msg"] for item in summary["unique_errors"]]
    warnings = [item["msg"] for item in summary["unique_warnings"]]

    haystack = (str(exc) + "\n" + tail).lower()
    if "license" in haystack and ("no such feature" in haystack or "failed" in haystack or "unavailable" in haystack or "cannot connect to license" in haystack):
        code = "license_unavailable"
    elif "cannot open file" in haystack or "unable to find model" in haystack or "undefined model" in haystack:
        code = "model_not_found"
    elif "undefined" in haystack or "unresolved" in haystack:
        code = "undefined_reference"
    elif "syntax error" in haystack or "parser" in haystack:
        code = "netlist_syntax_error"
    elif "convergence" in haystack:
        code = "convergence_failed"
    elif errors or "hpeesofsim" in haystack or isinstance(exc, RuntimeError):
        code = "simulation_failed"
    else:
        code = type(exc).__name__

    msg = str(exc).strip()
    if errors and errors[0] not in msg:
        msg = f"{msg} | {errors[0]}" if msg else errors[0]
    return code, msg[:2000], errors[:15], warnings[:15], summary


def _extract_sim_warnings(sim_log_path: Path) -> list[str]:
    if not sim_log_path.is_file():
        return []
    try:
        tail = sim_log_path.read_text(encoding="utf-8", errors="replace")[-16000:]
    except OSError:
        return []
    out = []
    for ln in tail.splitlines():
        s = ln.strip()
        if "warning detected by hpeesofsim" in s.lower() or s.lower().startswith("warning:"):
            out.append(s[:300])
    return out[:15]


def _restore_if_cancelled(job: Path, status_path: Path, base_status: dict[str, Any] | None = None) -> bool:
    cancel_path = job / "cancel.flag"
    if cancel_path.is_file():
        merged = dict(base_status or read_json(status_path))
        try:
            cancel_meta = read_json(cancel_path)
            if isinstance(cancel_meta, dict):
                merged.update(cancel_meta)
        except (OSError, ValueError, TypeError):
            merged["state"] = "cancelled"
        merged.setdefault("worker_pid", os.getpid())
        write_json(status_path, merged)
        return True
    try:
        latest = read_json(status_path)
    except OSError:
        return False
    return latest.get("state") == "cancelled"


def run(job: Path) -> int:
    status_path = job / "status.json"
    pid_path = job / "worker.pid"
    if not pid_path.is_file():
        write_json(pid_path, {"pid": os.getpid()})
    status = read_json(status_path)
    if _restore_if_cancelled(job, status_path, status):
        return 1
    started_at = time.time()
    status.update(state="running", started_at=started_at, worker_pid=os.getpid())
    write_json(status_path, status)
    # Eliminate TOCTOU window if job_cancel wrote cancel.flag right before status.json became 'running'.
    if _restore_if_cancelled(job, status_path, status):
        return 1
    sim_log = job / "sim.log"
    try:
        from keysight.ads import dataset
        from keysight.edatoolbox import ads

        # Clean any stale .ds inside this isolated job dir before invoking hpeesofsim
        for old_ds in job.glob("*.ds"):
            old_ds.unlink(missing_ok=True)

        spec = status["request"]
        context = status.get("context", {})
        netlist_text = (job / "snapshot.ckt").read_text(encoding="utf-8")
        ads.CircuitSimulator().run_netlist(
            netlist_text,
            output_dir=str(job),
            output_file=str(sim_log),
            rel_data_dir=context.get("data_dir"),
            pdk_dirs=context.get("model_dirs") or None,
        )
        if _restore_if_cancelled(job, status_path, status):
            return 1
        files = [
            f for f in job.glob("*.ds")
            if f.is_file() and f.stat().st_size > 0 and f.stat().st_mtime >= started_at - 2.0
        ]
        if len(files) != 1:
            raise RuntimeError(f"Expected exactly one newly generated ADS dataset (.ds), found {len(files)}.")

        ds_file = files[0]
        with dataset.open(ds_file) as opened:
            write_json(job / "catalog.json", describe(opened))
            if spec.get("block"):
                write_json(job / "result.json", read_trace(opened, spec["block"], spec["axis"], spec["variable"]))

        log_text = sim_log.read_text(encoding="utf-8", errors="replace")[-32000:] if sim_log.is_file() else ""
        log_summary = distill_simulation_log(log_text)
        warnings = [w["msg"] for w in log_summary["unique_warnings"]]
        status.update(
            state="succeeded",
            dataset=str(ds_file),
            dataset_bytes=ds_file.stat().st_size,
            warnings=warnings,
            log_summary=log_summary,
        )
        if spec.get("block"):
            status["result"] = str(job / "result.json")
    except Exception as exc:
        # Respect explicit external cancellation if status was marked cancelled
        if _restore_if_cancelled(job, status_path, status):
            return 1
        code, message, errors, warnings, log_summary = _classify_sim_failure(exc, sim_log)
        status.update(
            state="failed",
            code=code,
            message=message,
            sim_errors=errors,
            log_summary=log_summary,
            warnings=warnings,
            log=str(sim_log),
        )
    if _restore_if_cancelled(job, status_path, status):
        return 1
    status["finished_at"] = time.time()
    write_json(status_path, status)
    return int(status["state"] != "succeeded")


def finite_or_null(value: Any) -> float | None:
    value = float(value)
    return value if math.isfinite(value) else None


def describe(ds: Any) -> dict[str, Any]:
    return {
        "blocks": [
            {
                "name": block.name,
                "axes": [axis.name for axis in block.ivars],
                "variables": [variable.name for variable in block.dvars],
            }
            for block in ds.values()
        ]
    }


def _filtered_dataframe(ds: Any, block: str, axis: str, filters: dict[str, Any] | None) -> tuple[Any, set[str], set[str]]:
    if block not in ds:
        raise ValueError(f"Block '{block}' not found in dataset. Available: {[b.name for b in ds.values()]}")
    selected = ds[block]
    axes = {item.name for item in selected.ivars}
    dvars = {item.name for item in selected.dvars}
    if axis not in axes:
        raise ValueError(f"Axis '{axis}' not in block '{block}' axes {sorted(axes)}.")
    table = selected.to_dataframe().reset_index()
    for key, value in (filters or {}).items():
        if key not in axes or key == axis:
            raise ValueError("Filters must name independent axes other than the requested axis.")
        table = table[table[key] == value]
    return table, axes, dvars


def read_trace(
    ds: Any,
    block: str,
    axis: str,
    variable: str,
    offset: int = 0,
    limit: int | None = None,
    filters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    table, _, dvars = _filtered_dataframe(ds, block, axis, filters)
    if variable not in dvars:
        raise ValueError("Select an axis and dependent variable from the dataset catalog.")
    count = len(table)
    if limit is None:
        size = min(count, 1001)
        indexes = [i * (count - 1) // (size - 1) for i in range(size)] if size > 1 else list(range(size))
        page = table.iloc[indexes]
        selection = "evenly spaced row indices including endpoints"
    else:
        page = table.iloc[offset:offset + limit]
        selection = "consecutive rows after applying filters"
    samples = []
    for coordinate, value in zip(page[axis], page[variable]):
        cval = complex(value)
        samples.append([finite_or_null(coordinate), finite_or_null(cval.real), finite_or_null(cval.imag)])
    return {
        "block": block,
        "columns": [axis, variable + ".real", variable + ".imag"],
        "total_points": count,
        "selection": selection,
        "offset": offset,
        "samples": samples,
        "dataset": str(ds.path),
    }


def json_cell(value: Any) -> Any:
    if isinstance(value, (str, bool, int)) or value is None:
        return value
    if hasattr(value, "item"):
        value = value.item()
        if isinstance(value, (str, bool, int)):
            return value
    if isinstance(value, complex):
        return {"real": finite_or_null(value.real), "imag": finite_or_null(value.imag)}
    return finite_or_null(value)


def read_table(ds: Any, block: str, columns: list[str], offset: int, limit: int) -> dict[str, Any]:
    table = ds[block].to_dataframe().reset_index()
    if any(name not in table for name in columns):
        raise ValueError("One or more columns do not exist in this block.")
    page = table[columns].iloc[offset:offset + limit]
    return {
        "block": block,
        "columns": columns,
        "offset": offset,
        "total_points": len(table),
        "rows": [[json_cell(value) for value in row] for row in page.itertuples(index=False, name=None)],
    }


def _db20(mag: float) -> float | None:
    if not math.isfinite(mag) or mag <= 0.0:
        return None
    return 20.0 * math.log10(mag)


def _interpolate_complex(xs: list[float], ys: list[complex], x_target: float) -> tuple[complex, float, bool]:
    """Return (interpolated_or_endpoint_complex, nearest_coordinate_x, in_range)."""
    nearest_idx = min(range(len(xs)), key=lambda idx: abs(xs[idx] - x_target))
    nearest_x = xs[nearest_idx]
    x_min = min(xs)
    x_max = max(xs)
    tol = 1e-12 * max(1.0, abs(x_min), abs(x_max))
    if x_target < x_min - tol or x_target > x_max + tol:
        return ys[nearest_idx], nearest_x, False
    for i in range(len(xs) - 1):
        x0, x1 = xs[i], xs[i + 1]
        low, high = (x0, x1) if x0 <= x1 else (x1, x0)
        if low - tol <= x_target <= high + tol:
            span = x1 - x0
            t = (x_target - x0) / span if abs(span) > 0 else 0.0
            t = min(1.0, max(0.0, t))
            return ys[i] + t * (ys[i + 1] - ys[i]), nearest_x, True
    return ys[nearest_idx], nearest_x, True


def _estimate_3db_crossings(xs: list[float], mags: list[float]) -> list[float]:
    if len(xs) < 2:
        return []
    peak = max(mags)
    if peak <= 0.0:
        return []
    target_mag = peak / math.sqrt(2.0)
    crossings: list[float] = []
    for i in range(len(xs) - 1):
        m0, m1 = mags[i], mags[i + 1]
        if (m0 - target_mag) * (m1 - target_mag) <= 0.0 and abs(m1 - m0) > 1e-18:
            t = (target_mag - m0) / (m1 - m0)
            x0, x1 = xs[i], xs[i + 1]
            # Log-axis interpolation when axis is strictly positive and spans decades
            if x0 > 0 and x1 > 0 and (x1 / x0 > 1.05 or x0 / x1 > 1.05):
                x_cross = math.exp(math.log(x0) + t * (math.log(x1) - math.log(x0)))
            else:
                x_cross = x0 + t * (x1 - x0)
            crossings.append(x_cross)
    return crossings


def analyze_dataset(
    ds: Any,
    block: str,
    axis: str,
    signals: list[str] | None = None,
    at: list[float] | None = None,
    filters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compute engineering metrics, -3 dB crossings, S-parameter checks, and point interpolations."""
    table, _, dvars = _filtered_dataframe(ds, block, axis, filters)
    chosen = signals if signals else sorted(dvars)[:16]
    missing = [s for s in chosen if s not in dvars]
    if missing:
        raise ValueError(f"Signal(s) {missing} do not exist in block '{block}'. Available: {sorted(dvars)}")

    xs = [float(v) for v in table[axis]]
    if not xs:
        raise ValueError(f"Block '{block}' produced 0 points after applying filters.")

    metrics: dict[str, Any] = {}
    for sig in chosen:
        cvals = [complex(v) for v in table[sig]]
        mags = [abs(c) for c in cvals]
        phases = [math.degrees(math.atan2(c.imag, c.real)) for c in cvals]
        is_pure_real = all(abs(c.imag) <= 1e-15 * max(1.0, abs(c.real)) for c in cvals)
        peak_idx = max(range(len(mags)), key=lambda idx: mags[idx])
        min_idx = min(range(len(mags)), key=lambda idx: mags[idx])

        sig_info: dict[str, Any] = {
            "points": len(cvals),
            "is_complex": not is_pure_real,
            "mag_min": finite_or_null(mags[min_idx]),
            "mag_max": finite_or_null(mags[peak_idx]),
            "axis_at_max": finite_or_null(xs[peak_idx]),
            "axis_at_min": finite_or_null(xs[min_idx]),
            "db20_max": finite_or_null(_db20(mags[peak_idx])) if mags[peak_idx] > 0 else None,
            "db20_min": finite_or_null(_db20(mags[min_idx])) if mags[min_idx] > 0 else None,
            "start_value": {"real": finite_or_null(cvals[0].real), "imag": finite_or_null(cvals[0].imag)},
            "end_value": {"real": finite_or_null(cvals[-1].real), "imag": finite_or_null(cvals[-1].imag)},
            "phase_deg_start": finite_or_null(phases[0]),
            "phase_deg_end": finite_or_null(phases[-1]),
            "crossings_3db": [finite_or_null(c) for c in _estimate_3db_crossings(xs, mags)[:8]],
        }
        if is_pure_real:
            reals = [c.real for c in cvals]
            mean_v = sum(reals) / len(reals)
            rms_v = math.sqrt(sum(r * r for r in reals) / len(reals))
            sig_info.update({
                "real_min": finite_or_null(min(reals)),
                "real_max": finite_or_null(max(reals)),
                "real_mean": finite_or_null(mean_v),
                "real_rms": finite_or_null(rms_v),
                "pk_to_pk": finite_or_null(max(reals) - min(reals)),
            })
        if at:
            sampled = []
            x_tol = 1e-12 * max(1.0, abs(min(xs)), abs(max(xs)))
            for target_x in at[:200]:
                tx = float(target_x)
                interp_c, nearest_x, in_range = _interpolate_complex(xs, cvals, tx)
                mag = abs(interp_c)
                if in_range:
                    sampled.append({
                        "at": finite_or_null(tx),
                        "in_range": True,
                        "status": "exact" if abs(tx - nearest_x) <= x_tol else "interpolated",
                        "nearest_axis": finite_or_null(nearest_x),
                        "real": finite_or_null(interp_c.real),
                        "imag": finite_or_null(interp_c.imag),
                        "magnitude": finite_or_null(mag),
                        "db20": finite_or_null(_db20(mag)) if mag > 0 else None,
                        "phase_deg": finite_or_null(math.degrees(math.atan2(interp_c.imag, interp_c.real))),
                    })
                else:
                    sampled.append({
                        "at": finite_or_null(tx),
                        "in_range": False,
                        "status": "out_of_range",
                        "nearest_axis": finite_or_null(nearest_x),
                        "real": None,
                        "imag": None,
                        "magnitude": None,
                        "db20": None,
                        "phase_deg": None,
                        "clamped_endpoint": {
                            "axis": finite_or_null(nearest_x),
                            "real": finite_or_null(interp_c.real),
                            "imag": finite_or_null(interp_c.imag),
                            "magnitude": finite_or_null(mag),
                            "db20": finite_or_null(_db20(mag)) if mag > 0 else None,
                            "phase_deg": finite_or_null(math.degrees(math.atan2(interp_c.imag, interp_c.real))),
                        },
                    })
            sig_info["samples_at"] = sampled
        metrics[sig] = sig_info

    # Optional 2-port S-parameter sanity check (reciprocity + passivity)
    sparam_summary: dict[str, Any] | None = None
    if {"S[1,1]", "S[2,1]", "S[1,2]", "S[2,2]"} <= dvars:
        s11 = [complex(v) for v in table["S[1,1]"]]
        s21 = [complex(v) for v in table["S[2,1]"]]
        s12 = [complex(v) for v in table["S[1,2]"]]
        s22 = [complex(v) for v in table["S[2,2]"]]
        reciprocity_err = max(abs(a - b) for a, b in zip(s21, s12))
        # Largest singular value of 2x2 matrix S at each frequency
        max_sv = 0.0
        for a11, a21, a12, a22 in zip(s11, s21, s12, s22):
            # Frobenius + determinant closed form for 2x2 singular values
            frob_sq = abs(a11) ** 2 + abs(a12) ** 2 + abs(a21) ** 2 + abs(a22) ** 2
            det = a11 * a22 - a12 * a21
            disc = max(0.0, frob_sq ** 2 - 4.0 * (abs(det) ** 2))
            sv1 = math.sqrt(max(0.0, 0.5 * (frob_sq + math.sqrt(disc))))
            max_sv = max(max_sv, sv1)
        sparam_summary = {
            "two_port_detected": True,
            "is_passive": max_sv <= 1.0 + 1e-5,
            "max_singular_value": finite_or_null(max_sv),
            "is_reciprocal": reciprocity_err <= 1e-5,
            "max_reciprocity_error": finite_or_null(reciprocity_err),
        }

    x_min = min(xs)
    x_max = max(xs)
    x_tol = 1e-12 * max(1.0, abs(x_min), abs(x_max))
    out_of_range_at = [
        finite_or_null(float(tx))
        for tx in (at or [])[:200]
        if float(tx) < x_min - x_tol or float(tx) > x_max + x_tol
    ]

    return {
        "block": block,
        "axis": axis,
        "axis_start": finite_or_null(xs[0]),
        "axis_stop": finite_or_null(xs[-1]),
        "axis_min": finite_or_null(x_min),
        "axis_max": finite_or_null(x_max),
        "out_of_range_at": out_of_range_at,
        "total_points": len(xs),
        "signals": metrics,
        "sparam_2port": sparam_summary,
        "dataset": str(ds.path),
    }


def _execute_headless(spec: dict[str, Any]) -> dict[str, Any]:
    """Execute one or more ADS workspace/design/layout operations in standalone ADS Python."""
    from keysight.ads import ael, de
    from keysight.ads.de import db_uu
    import design_ops

    if de.is_pde_app():
        raise BridgeError("invalid_environment", "Headless worker cannot run inside the ADS GUI process.")

    ws_path = Path(spec["workspace_path"]).resolve()
    write = bool(spec.get("write", False))
    create_ws = bool(spec.get("create_workspace", False))
    artifact_dir = Path(spec["artifact_dir"]).resolve()

    if create_ws and not ws_path.exists():
        if not write:
            raise BridgeError("write_disabled", "Enable write=True to create a new ADS workspace.")
        ws_path.parent.mkdir(parents=True, exist_ok=True)
        workspace = de.create_workspace(ws_path)
        workspace.open()
    else:
        if not ws_path.is_dir() or not (ws_path / "lib.defs").is_file():
            raise BridgeError("invalid_workspace", f"Workspace '{ws_path}' does not exist or lacks lib.defs.")
        workspace = de.open_workspace(ws_path)

    opened_designs: list[Any] = []

    def track_design(d: Any) -> Any:
        opened_designs.append(d)
        return d

    def run_op(op: str, args: dict[str, Any]) -> dict[str, Any]:
        if op == "workspace.inspect":
            return {
                "workspace_path": str(ws_path),
                "in_ads_gui": False,
                "libraries": sorted(workspace.library_names),
                "writable_libraries": sorted(workspace.writable_library_names),
            }
        if op == "catalog.list":
            return design_ops.catalog(
                workspace,
                args.get("library"),
                args.get("search", ""),
                args.get("offset", 0),
                args.get("limit", 100),
            )
        if op == "component.describe":
            master = args.get("master")
            if not master and args.get("library") and args.get("cell"):
                master = f"{args['library']}:{args['cell']}"
            if not master:
                raise BridgeError("invalid_argument", "component.describe requires 'master' (e.g. 'ads_sources:V_DC') or both 'library' and 'cell'.")
            return design_ops.describe_component(workspace, str(master), compact=bool(args.get("compact", False)))
        if op == "design.list":
            views = []
            for lib_name in workspace.writable_library_names:
                for cell in workspace.open_library(lib_name).cells:
                    for v in cell.views:
                        views.append(str(v.lcv_name))
            return {"views": sorted(views), "limit_reached": False}
        if op == "library.create":
            if not write:
                raise BridgeError("write_disabled", "Headless call did not authorize workspace writes.")
            return design_ops.create_library(
                workspace,
                args["name"],
                bool(args.get("layout_technology", False)),
                lambda _state: None,
            )
        if op == "design.read":
            lib, cell, view = design_ops.split_lcv(args["design"], {"schematic"})
            if design_ops.get_view(workspace, lib, cell, view) is None:
                raise BridgeError("design_not_found", f"Design {args['design']} does not exist.")
            d = track_design(db_uu.open_design(args["design"], db_uu.DesignMode.READ_ONLY))
            return design_ops.read_schematic_details(d)
        if op == "design.check":
            lib, cell, view = design_ops.split_lcv(args["design"], {"schematic"})
            if design_ops.get_view(workspace, lib, cell, view) is None:
                raise BridgeError("design_not_found", f"Design {args['design']} does not exist.")
            d = track_design(db_uu.open_design(args["design"], db_uu.DesignMode.READ_ONLY))
            return design_ops.check_schematic(d, mode=args.get("mode", "tb"), search_dirs=args.get("search_dirs"))
        if op == "netlist.generate":
            lib, cell, view = design_ops.split_lcv(args["design"], {"schematic"})
            if design_ops.get_view(workspace, lib, cell, view) is None:
                raise BridgeError("design_not_found", f"Design {args['design']} does not exist.")
            d = track_design(db_uu.open_design(args["design"], db_uu.DesignMode.READ_ONLY))
            text = d.generate_netlist()
            import hashlib
            import netlist_parser
            rev = hashlib.sha256(text.encode("utf-8")).hexdigest()
            out: dict[str, Any] = {"design": args["design"], "revision": rev, "text": text}
            if args.get("inspect", True):
                out["parsed"] = netlist_parser.inspect_netlist(text)
            return out
        if op == "design.build":
            if not write:
                raise BridgeError("write_disabled", "Headless call did not authorize workspace writes.")
            design_name = args["design"]
            lib, cell, view = design_ops.split_lcv(design_name, {"schematic"})
            design_ops.new_name(cell)
            if lib not in workspace.writable_library_names:
                raise BridgeError("write_disabled", f"Library {lib} is not writable.")
            exists = design_ops.get_view(workspace, lib, cell, view) is not None
            if exists and not args.get("replace", False):
                raise BridgeError("design_exists", f"{design_name} already exists.")
            plan = design_ops.schematic_plan(
                workspace,
                args["instances"],
                args["wires"],
                pins=args.get("pins"),
                variables=args.get("variables"),
            )
            target = track_design(
                db_uu.open_design(design_name, db_uu.DesignMode.APPEND) if exists else db_uu.create_schematic(design_name)
            )
            try:
                with db_uu.Transaction(target, command="ADS Bridge headless build") as tx:
                    if exists:
                        target.clear_design()
                    design_ops.populate_schematic(target, plan)
                    tx.commit()
            except Exception:
                if not exists:
                    target.close_design()
                    opened_designs.remove(target)
                    workspace.open_library(lib).get_cell_if_exists(cell).delete_view(view)
                raise
            target.save_design()
            text = target.generate_netlist()
            import hashlib
            return {"design": design_name, "state": "saved", "revision": hashlib.sha256(text.encode("utf-8")).hexdigest()}
        if op == "design.update":
            if not write:
                raise BridgeError("write_disabled", "Headless call did not authorize workspace writes.")
            design_name = args["design"]
            lib, cell, view = design_ops.split_lcv(design_name, {"schematic"})
            if lib not in workspace.writable_library_names:
                raise BridgeError("write_disabled", f"Library {lib} is not writable.")
            if design_ops.get_view(workspace, lib, cell, view) is None:
                raise BridgeError("design_not_found", f"Design {design_name} does not exist.")
            target = track_design(db_uu.open_design(design_name, db_uu.DesignMode.APPEND))
            import hashlib
            expected_rev = args.get("revision")
            if expected_rev is not None:
                cur_rev = hashlib.sha256(target.generate_netlist().encode("utf-8")).hexdigest()
                if cur_rev != expected_rev:
                    raise BridgeError("design_changed", "The netlist revision changed. Read the design again.")
            with db_uu.Transaction(target, command="ADS Bridge headless update") as tx:
                applied = design_ops.apply_incremental_ops(workspace, target, args["ops"])
                tx.commit()
            target.save_design()
            new_rev = hashlib.sha256(target.generate_netlist().encode("utf-8")).hexdigest()
            return {"design": design_name, "state": "saved", "applied": applied, "revision": new_rev}
        if op == "layout.build":
            if not write:
                raise BridgeError("write_disabled", "Headless call did not authorize workspace writes.")
            design_name = args["design"]
            lib, cell, view = design_ops.split_lcv(design_name, {"layout"})
            design_ops.new_name(cell)
            if lib not in workspace.writable_library_names:
                raise BridgeError("write_disabled", f"Library {lib} is not writable.")
            if design_ops.get_view(workspace, lib, cell, view) is not None:
                raise BridgeError("design_exists", f"{design_name} already exists.")
            plan = design_ops.layout_plan(args["shapes"])
            target = track_design(db_uu.create_layout(design_name))
            with db_uu.Transaction(target, command="ADS Bridge headless layout") as tx:
                design_ops.populate_layout(target, plan)
                tx.commit()
            target.save_design()
            return {"design": design_name, "state": "saved"}
        if op == "layout.read":
            lib, cell, view = design_ops.split_lcv(args["design"], {"layout"})
            if design_ops.get_view(workspace, lib, cell, view) is None:
                raise BridgeError("design_not_found", f"Layout {args['design']} does not exist.")
            target = track_design(db_uu.open_design(args["design"], db_uu.DesignMode.READ_ONLY))
            limit = int(args.get("limit", 100))
            from itertools import islice
            shapes = [
                {"type": type(s).__name__, "layer": str(s.layer_id), "bbox": design_ops.box_values(s.bbox)}
                for s in islice(db_uu.ShapeIter(target), limit + 1)
            ]
            return {
                "design": args["design"],
                "units": "ADS layout user units",
                "bbox": design_ops.box_values(target.bbox),
                "shapes": shapes[:limit],
                "limit_reached": len(shapes) > limit,
            }
        if op == "layout.export_gds":
            lib, cell, view = design_ops.split_lcv(args["design"], {"layout"})
            if design_ops.get_view(workspace, lib, cell, view) is None:
                raise BridgeError("design_not_found", f"Layout {args['design']} does not exist.")
            target = track_design(db_uu.open_design(args["design"], db_uu.DesignMode.READ_ONLY))
            artifact_dir.mkdir(parents=True, exist_ok=True)
            import uuid
            gds_path = artifact_dir / f"{uuid.uuid4().hex}.gds"
            ael.call.gds_export_design(None, target, str(gds_path))
            if not gds_path.is_file() or gds_path.stat().st_size == 0:
                raise BridgeError("export_failed", "ADS did not produce a GDS file.")
            return {"design": args["design"], "artifact": str(gds_path), "bytes": gds_path.stat().st_size}
        raise BridgeError("unknown_operation", f"Unsupported headless operation: {op}")

    try:
        if "steps" in spec:
            continue_on_error = bool(spec.get("continue_on_error", False))
            step_results: list[dict[str, Any]] = []
            for idx, s in enumerate(spec["steps"]):
                op_name = s.get("op")
                if not op_name:
                    err_msg = f"Workflow step {idx} is missing 'op' (got keys: {sorted(s.keys())})."
                    if continue_on_error:
                        step_results.append({"step": idx, "op": None, "ok": False, "error": {"code": "invalid_argument", "message": err_msg}})
                        continue
                    raise BridgeError("invalid_argument", err_msg)
                try:
                    step_results.append(run_op(str(op_name), s.get("args", {})))
                except BridgeError as exc:
                    if continue_on_error:
                        step_results.append({"step": idx, "op": op_name, "ok": False, "error": {"code": exc.code, "message": str(exc)}})
                    else:
                        raise BridgeError(exc.code, f"[step {idx}: {op_name}] {exc}") from exc
                except KeyError as exc:
                    msg = f"[step {idx}: {op_name}] Missing required argument {exc}."
                    if continue_on_error:
                        step_results.append({"step": idx, "op": op_name, "ok": False, "error": {"code": "invalid_argument", "message": msg}})
                    else:
                        raise BridgeError("invalid_argument", msg) from exc
            return {"workspace_path": str(ws_path), "steps": step_results}
        return run_op(spec["op"], spec.get("args", {}))
    finally:
        for d in reversed(opened_designs):
            try:
                if getattr(d, "is_valid", False):
                    d.close_design()
            except Exception:
                pass
        if de.workspace_is_open():
            try:
                de.close_workspace()
            except Exception:
                pass


def extract_block(
    ds: Any,
    block: str | None = None,
    axis: str | None = None,
    signals: list[str] | None = None,
    filters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Extract full axis + complex signal arrays from a dataset block for bridge-layer RF compression."""
    blocks = list(ds.values())
    if not blocks:
        raise ValueError("Dataset contains no data blocks.")
    chosen_block = block or blocks[0].name
    if chosen_block not in ds:
        raise ValueError(f"Block '{chosen_block}' not found in dataset. Available: {[b.name for b in blocks]}")
    selected = ds[chosen_block]
    ivars = [iv.name for iv in selected.ivars]
    if not ivars:
        raise ValueError(f"Block '{chosen_block}' has no independent axis.")
    chosen_axis = axis or ("freq" if "freq" in ivars else ("time" if "time" in ivars else ivars[-1]))
    table, _, dvars = _filtered_dataframe(ds, chosen_block, chosen_axis, filters)
    applied_filters = set((filters or {}).keys())
    outer_axes: dict[str, list[Any]] = {}
    for iv in ivars:
        if iv != chosen_axis and iv not in applied_filters and iv in table.columns:
            uniq = []
            seen_u: set[Any] = set()
            for v in table[iv]:
                fv = finite_or_null(v) if isinstance(v, (int, float)) else str(v)
                if fv not in seen_u:
                    seen_u.add(fv)
                    uniq.append(fv)
            if len(uniq) > 1:
                outer_axes[iv] = uniq[:64]
    chosen_signals = signals if signals else sorted(dvars)
    missing = [s for s in chosen_signals if s not in dvars]
    if missing:
        raise ValueError(f"Signal(s) {missing} not in block '{chosen_block}'. Available: {sorted(dvars)}")

    xs = [finite_or_null(v) for v in table[chosen_axis]]
    sig_out: dict[str, list[list[float | None]]] = {}
    skipped_non_numeric: list[str] = []
    explicit_request = bool(signals)
    for sname in chosen_signals:
        pairs = []
        non_numeric = False
        for raw_v in table[sname]:
            if isinstance(raw_v, (str, bytes)):
                non_numeric = True
                break
            try:
                cv = complex(raw_v)
            except (TypeError, ValueError):
                non_numeric = True
                break
            pairs.append([finite_or_null(cv.real), finite_or_null(cv.imag)])
        if non_numeric:
            if explicit_request:
                raise ValueError(f"Requested signal '{sname}' in block '{chosen_block}' is non-numeric.")
            skipped_non_numeric.append(sname)
            continue
        sig_out[sname] = pairs
    if not sig_out:
        raise ValueError(f"Block '{chosen_block}' has no numeric signals (skipped: {skipped_non_numeric}).")
    return {
        "block": chosen_block,
        "axis": chosen_axis,
        "ivars": ivars,
        "outer_axes": outer_axes,
        "axis_values": xs,
        "signals": sig_out,
        "skipped_non_numeric": skipped_non_numeric,
        "dataset": str(ds.path),
    }


def extract_sweep(
    ds: Any,
    sweep_axis: str,
    block: str | None = None,
    axis: str | None = None,
    signals: list[str] | None = None,
    filters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Extract all slices of a 2D ParamSweep block along `sweep_axis` in a single ADS dataset open."""
    blocks = list(ds.values())
    if not blocks:
        raise ValueError("Dataset contains no data blocks.")
    chosen_block = block or blocks[0].name
    if chosen_block not in ds:
        raise ValueError(f"Block '{chosen_block}' not found in dataset. Available: {[b.name for b in blocks]}")
    selected = ds[chosen_block]
    ivars = [iv.name for iv in selected.ivars]
    if sweep_axis not in ivars:
        raise ValueError(f"sweep_axis '{sweep_axis}' not found in block '{chosen_block}'. Available axes: {ivars}")
    chosen_axis = axis or ("freq" if "freq" in ivars and sweep_axis != "freq" else ("time" if "time" in ivars and sweep_axis != "time" else [iv for iv in ivars if iv != sweep_axis][-1]))
    table, _, dvars = _filtered_dataframe(ds, chosen_block, chosen_axis, filters)
    chosen_signals = signals if signals else sorted(dvars)
    missing = [s for s in chosen_signals if s not in dvars]
    if missing:
        raise ValueError(f"Signal(s) {missing} not in block '{chosen_block}'. Available: {sorted(dvars)}")

    # Group rows by sweep_axis preserving original sweep order
    ordered_keys: list[Any] = []
    buckets: dict[Any, list[int]] = {}
    for row_idx, raw_sv in enumerate(table[sweep_axis]):
        sv = finite_or_null(raw_sv) if isinstance(raw_sv, (int, float)) else str(raw_sv)
        if sv not in buckets:
            ordered_keys.append(sv)
            buckets[sv] = []
        buckets[sv].append(row_idx)

    explicit_request = bool(signals)
    slices_out: list[dict[str, Any]] = []
    skipped_non_numeric: list[str] = []
    for sv in ordered_keys:
        idxs = buckets[sv]
        sub_xs = [finite_or_null(table[chosen_axis].iloc[i]) for i in idxs]
        sub_sigs: dict[str, list[list[float | None]]] = {}
        for sname in chosen_signals:
            pairs = []
            non_numeric = False
            col = table[sname]
            for i in idxs:
                raw_v = col.iloc[i]
                if isinstance(raw_v, (str, bytes)):
                    non_numeric = True
                    break
                try:
                    cv = complex(raw_v)
                except (TypeError, ValueError):
                    non_numeric = True
                    break
                pairs.append([finite_or_null(cv.real), finite_or_null(cv.imag)])
            if non_numeric:
                if explicit_request:
                    raise ValueError(f"Requested signal '{sname}' in block '{chosen_block}' is non-numeric.")
                if sname not in skipped_non_numeric:
                    skipped_non_numeric.append(sname)
                continue
            sub_sigs[sname] = pairs
        slices_out.append({"sweep_value": sv, "axis_values": sub_xs, "signals": sub_sigs})

    return {
        "block": chosen_block,
        "axis": chosen_axis,
        "sweep_axis": sweep_axis,
        "ivars": ivars,
        "slices": slices_out,
        "skipped_non_numeric": skipped_non_numeric,
        "dataset": str(ds.path),
    }


def _execute_dds_build(spec: dict[str, Any]) -> dict[str, Any]:
    """Build a native Keysight ADS `.dds` Data Display file via `keysight.ads.dds`."""
    import keysight.ads.dds as dds  # Must precede any keysight.ads.dataset import

    ws_path = Path(spec["workspace_path"]).resolve()
    dataset_name = str(spec["dataset_name"])
    output_dds = Path(spec["output_dds"])
    if not output_dds.is_absolute():
        output_dds = (ws_path / output_dds).resolve()
    pages_spec = spec.get("pages") or []
    if not isinstance(pages_spec, list) or not 1 <= len(pages_spec) <= 16:
        raise BridgeError("invalid_argument", "pages must be a list of 1 through 16 page specifications.")

    dds_file = dds.new_dds_file(dataset_name, ws_path)
    eq_errors: list[dict[str, str]] = []
    eq_count = 0
    plot_count = 0
    page_names: list[str] = []

    for p_idx, p_spec in enumerate(pages_spec):
        page_name = str(p_spec.get("name") or f"Page_{p_idx + 1}")
        page = dds_file.pages[0] if p_idx == 0 else dds_file.new_page(page_name)
        page.name = page_name
        page_names.append(page_name)

        for eq_spec in p_spec.get("equations", ()):
            lhs = str(eq_spec["name"])
            rhs = str(eq_spec["expr"])
            eq_obj = page.add_equation(lhs, rhs)
            eq_count += 1
            err_msg = str(getattr(eq_obj, "errors", "") or "").strip()
            if err_msg:
                eq_errors.append({"page": page_name, "equation": lhs, "error": err_msg})

        created_plots = []
        for plt_spec in p_spec.get("plots", ()):
            kind = str(plt_spec.get("kind", "rect")).strip().lower()
            if kind == "smith":
                plt_obj = page.add_smith_chart()
            elif kind == "polar":
                plt_obj = page.add_polar_plot()
            elif kind == "list":
                plt_obj = page.add_list()
            else:
                plt_obj = page.add_plot()
            if plt_spec.get("title") and hasattr(plt_obj, "title"):
                plt_obj.title = str(plt_spec["title"])
            traces = [str(t) for t in plt_spec.get("traces", ())]
            if traces:
                plt_obj.add_traces(traces)
            if plt_spec.get("legend", True) and hasattr(plt_obj, "add_legend"):
                plt_obj.add_legend()
            created_plots.append(plt_obj)
            plot_count += 1

        if len(created_plots) >= 2:
            cols = int(p_spec.get("cols") or (2 if len(created_plots) >= 2 else 1))
            rows = int(math.ceil(len(created_plots) / max(1, cols)))
            page.align_grid(created_plots, rows, cols)

    if dds_file.windows:
        dds_file.windows[0].current_page = page_names[0]
        dds_file.windows[0].zoom_all()

    output_dds.parent.mkdir(parents=True, exist_ok=True)
    dds_file.save(str(output_dds))
    return {
        "dds_path": str(output_dds),
        "dataset": dataset_name,
        "pages": page_names,
        "plot_count": plot_count,
        "equation_count": eq_count,
        "equation_errors": eq_errors,
        "ok": len(eq_errors) == 0,
    }


SERVE_JSON_PREFIX = "__ADS_BRIDGE_JSON__:"


def _serve_persistent_loop() -> int:
    """Persistent stdio worker that imports Keysight ADS Python modules once and serves many sims/extractions."""
    from keysight.ads import dataset
    from keysight.edatoolbox import ads

    simulator = ads.CircuitSimulator()
    sys.stdout.write(SERVE_JSON_PREFIX + json.dumps({"ready": True, "pid": os.getpid()}) + "\n")
    sys.stdout.flush()

    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError as exc:
            sys.stdout.write(SERVE_JSON_PREFIX + json.dumps({"ok": False, "error": f"invalid_json: {exc}"}) + "\n")
            sys.stdout.flush()
            continue

        op = req.get("op")
        if op == "quit":
            sys.stdout.write(SERVE_JSON_PREFIX + json.dumps({"ok": True, "bye": True}) + "\n")
            sys.stdout.flush()
            return 0

        if op == "sim_extract":
            job = Path(req["job_dir"]).resolve()
            job.mkdir(parents=True, exist_ok=True)
            status_path = job / "status.json"
            sim_log = job / "sim.log"
            started_at = time.time()
            status: dict[str, Any] = {
                "job_id": req["job_id"],
                "session_id": req["session_id"],
                "request": req.get("request", {}),
                "context": req.get("context", {}),
                "ads_root": req.get("ads_root"),
                "state": "running",
                "submitted_at": started_at,
                "started_at": started_at,
                "worker_pid": os.getpid(),
            }
            try:
                netlist_text = req["netlist"]
                (job / "snapshot.ckt").write_text(netlist_text, encoding="utf-8", newline="")
                for old_ds in job.glob("*.ds"):
                    old_ds.unlink(missing_ok=True)
                context = status["context"]
                simulator.run_netlist(
                    netlist_text,
                    output_dir=str(job),
                    output_file=str(sim_log),
                    rel_data_dir=context.get("data_dir"),
                    pdk_dirs=context.get("model_dirs") or None,
                )
                files = [
                    f for f in job.glob("*.ds")
                    if f.is_file() and f.stat().st_size > 0 and f.stat().st_mtime >= started_at - 2.0
                ]
                if len(files) != 1:
                    raise RuntimeError(f"Expected 1 newly generated .ds dataset, found {len(files)}.")
                ds_file = files[0]
                extracted: dict[str, Any] = {}
                with dataset.open(ds_file) as opened:
                    if req.get("write_catalog", False):
                        write_json(job / "catalog.json", describe(opened))
                    for item in req.get("extract", ()):
                        ekey = item["key"]
                        extracted[ekey] = extract_block(
                            opened,
                            block=item.get("block"),
                            axis=item.get("axis"),
                            signals=item.get("signals"),
                            filters=item.get("filters"),
                        )
                log_text = sim_log.read_text(encoding="utf-8", errors="replace")[-32000:] if sim_log.is_file() else ""
                log_summary = distill_simulation_log(log_text)
                status.update(
                    state="succeeded",
                    dataset=str(ds_file),
                    dataset_bytes=ds_file.stat().st_size,
                    warnings=[w["msg"] for w in log_summary["unique_warnings"]],
                    log_summary=log_summary,
                    finished_at=time.time(),
                )
                write_json(status_path, status)
                resp = {"ok": True, "status": status, "extracted": extracted}
            except Exception as exc:
                code, message, errors, warnings, log_summary = _classify_sim_failure(exc, sim_log)
                status.update(
                    state="failed",
                    code=code,
                    message=message,
                    sim_errors=errors,
                    warnings=warnings,
                    log_summary=log_summary,
                    finished_at=time.time(),
                )
                if sim_log.is_file():
                    status["log"] = str(sim_log)
                write_json(status_path, status)
                resp = {"ok": True, "status": status, "extracted": {}}
            sys.stdout.write(SERVE_JSON_PREFIX + json.dumps(resp, allow_nan=False) + "\n")
            sys.stdout.flush()
            continue

        if op == "extract":
            try:
                ds_path = Path(req["dataset"]).resolve()
                with dataset.open(ds_path) as opened:
                    res = extract_block(
                        opened,
                        block=req.get("block"),
                        axis=req.get("axis"),
                        signals=req.get("signals"),
                        filters=req.get("filters"),
                    )
                resp = {"ok": True, "result": res}
            except Exception as exc:
                resp = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            sys.stdout.write(SERVE_JSON_PREFIX + json.dumps(resp, allow_nan=False) + "\n")
            sys.stdout.flush()
            continue

        if op == "extract_sweep":
            try:
                ds_path = Path(req["dataset"]).resolve()
                with dataset.open(ds_path) as opened:
                    res = extract_sweep(
                        opened,
                        sweep_axis=str(req["sweep_axis"]),
                        block=req.get("block"),
                        axis=req.get("axis"),
                        signals=req.get("signals"),
                        filters=req.get("filters"),
                    )
                resp = {"ok": True, "result": res}
            except Exception as exc:
                resp = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            sys.stdout.write(SERVE_JSON_PREFIX + json.dumps(resp, allow_nan=False) + "\n")
            sys.stdout.flush()
            continue

        sys.stdout.write(SERVE_JSON_PREFIX + json.dumps({"ok": False, "error": f"unknown_op: {op}"}) + "\n")
        sys.stdout.flush()
    return 0


def main() -> int:
    mode = sys.argv[1]
    if mode == "serve":
        return _serve_persistent_loop()
    if mode == "query":
        from keysight.ads import dataset

        with dataset.open(Path(sys.argv[2])) as opened:
            if len(sys.argv) == 4:
                args = json.loads(sys.argv[3])
                if args.pop("extract_sweep", False):
                    result = extract_sweep(opened, **args)
                elif args.pop("extract_block", False):
                    result = extract_block(opened, **args)
                elif args.pop("analyze", False):
                    result = analyze_dataset(opened, **args)
                elif args.pop("table", False):
                    result = read_table(opened, **args)
                else:
                    result = read_trace(opened, **args)
            else:
                result = describe(opened)
        print(json.dumps(result, allow_nan=False))
        return 0

    if mode == "dds":
        req_path = Path(sys.argv[2]).resolve()
        resp_path = Path(sys.argv[3]).resolve()
        try:
            spec = read_json(req_path)
            payload = {"result": _execute_dds_build(spec)}
            code = 0
        except BridgeError as exc:
            payload = {"error": {"code": exc.code, "message": str(exc)}}
            code = 1
        except Exception as exc:
            payload = {"error": {"code": "dds_build_failed", "message": f"{type(exc).__name__}: {exc}"}}
            code = 1
        write_json(resp_path, payload)
        return code

    if mode == "headless":
        req_path = Path(sys.argv[2]).resolve()
        resp_path = Path(sys.argv[3]).resolve()
        try:
            spec = read_json(req_path)
            payload = {"result": _execute_headless(spec)}
            code = 0
        except BridgeError as exc:
            payload = {"error": {"code": exc.code, "message": str(exc)}}
            code = 1
        except Exception as exc:
            payload = {"error": {"code": "ads_headless_error", "message": f"{type(exc).__name__}: {exc}"}}
            code = 1
        write_json(resp_path, payload)
        return code

    job = Path(mode).resolve()
    with (job / "worker.lock").open("a+b") as lock:
        lock.seek(0)
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        return run(job)


if __name__ == "__main__":
    raise SystemExit(main())
