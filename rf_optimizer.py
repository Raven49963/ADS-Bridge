"""Bridge-layer closed-loop automatic circuit parameter optimizer.

Drives 64-bit Keysight ADS `hpeesofsim` directly from the bridge layer using
NumPy + SciPy + scikit-rf so AI models submit a compact declarative objective
specification and receive only the optimized parameters and verified metrics JSON.
"""

from __future__ import annotations

import itertools
import json
import math
from pathlib import Path
import time
from typing import Any, Callable

import numpy as np

try:
    from scipy.optimize import minimize as scipy_minimize  # type: ignore
except ImportError:
    scipy_minimize = None  # type: ignore

import netlist_parser
import rf_compress


SUPPORTED_METHODS = {"nelder_mead", "powell", "grid_refine"}
SUPPORTED_OPS = {"eq", "le", "ge", "min", "max"}
_OP_ALIASES = {"==": "eq", "=": "eq", "<=": "le", ">=": "ge"}
_LINEAR_ZERO_METRICS = {"pout_w", "mag_at", "cutoff_3db_hz", "matched_bw_hz", "vswr_max", "rollett_k_min"}


def _sig_float(val: float, digits: int = 10) -> float:
    """Round to significant digits (never truncating pF/nH SI values like 5.1e-11 to 0.0)."""
    if not math.isfinite(val):
        return val
    return float(f"{val:.{digits}g}")


def _display_float(val: float, decimals: int = 6, sig_digits: int = 6) -> float | None:
    """Format a metric/margin for compact JSON without ever truncating tiny non-zero SI values to 0.0."""
    if not math.isfinite(val):
        return None
    if val == 0.0:
        return 0.0
    if abs(val) >= 10.0 ** (-decimals + 3):
        return round(val, decimals)
    return _sig_float(val, sig_digits)
SUPPORTED_METRICS = {
    "cutoff_3db_hz",
    "db20_at",
    "mag_at",
    "ripple_db",
    "s_db_max",
    "s_db_min",
    "s_db_at",
    "vswr_max",
    "matched_bw_hz",
    "rollett_k_min",
    "pout_dbm",
    "pout_w",
    "pae_percent",
    "drain_eff_percent",
    "h2_dbc",
    "thd_percent",
}


def _validate_parameters(parameters: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(parameters, list) or not 1 <= len(parameters) <= 12:
        raise ValueError("Provide between 1 and 12 optimization parameters.")
    cleaned: list[dict[str, Any]] = []
    seen_refs: set[str] = set()
    for p in parameters:
        if not isinstance(p, dict) or not isinstance(p.get("ref"), str) or not p["ref"].strip():
            raise ValueError("Each parameter must be an object with a non-empty 'ref' (e.g. 'R1.R' or 'VAR:Ce_val').")
        ref = p["ref"].strip()
        if ref in seen_refs:
            raise ValueError(f"Duplicate optimization parameter ref: {ref}")
        seen_refs.add(ref)
        lo = float(p["min"])
        hi = float(p["max"])
        if not (math.isfinite(lo) and math.isfinite(hi) and hi > lo):
            raise ValueError(f"Parameter '{ref}' requires finite bounds with max > min.")
        scale = str(p.get("scale", "linear")).strip().lower()
        if scale not in {"linear", "log"}:
            raise ValueError(f"Parameter '{ref}' scale must be 'linear' or 'log'.")
        if scale == "log" and lo <= 0.0:
            raise ValueError(f"Parameter '{ref}' with scale='log' requires min > 0.")
        init = p.get("init")
        if init is None:
            init_val = math.sqrt(lo * hi) if scale == "log" else 0.5 * (lo + hi)
        else:
            init_val = min(hi, max(lo, float(init)))
        unit = str(p.get("unit", "")).strip()
        cleaned.append({
            "ref": ref,
            "min": lo,
            "max": hi,
            "init": init_val,
            "scale": scale,
            "unit": unit,
        })
    return cleaned


def _validate_objectives(objectives: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(objectives, list) or not 1 <= len(objectives) <= 16:
        raise ValueError("Provide between 1 and 16 optimization objectives.")
    cleaned: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for idx, obj in enumerate(objectives):
        if not isinstance(obj, dict):
            raise ValueError("Each objective must be a JSON object.")
        metric = str(obj.get("metric", "")).strip().lower()
        if metric not in SUPPORTED_METRICS:
            raise ValueError(f"Unsupported objective metric {metric!r}. Supported: {sorted(SUPPORTED_METRICS)}")
        raw_op = str(obj.get("op", "eq")).strip().lower()
        op = _OP_ALIASES.get(raw_op, raw_op)
        if op not in SUPPORTED_OPS:
            raise ValueError(f"Unsupported objective op {raw_op!r}. Supported: {sorted(SUPPORTED_OPS)} (or ==, <=, >=).")
        target = obj.get("target")
        if op in {"eq", "le", "ge"} and target is None:
            raise ValueError(f"Objective '{metric}' with op='{op}' requires numeric 'target'.")
        target_f = float(target) if target is not None else None
        if target_f is not None and not math.isfinite(target_f):
            raise ValueError(f"Objective '{metric}' target must be finite; got {target!r}.")
        weight = float(obj.get("weight", 1.0))
        if not math.isfinite(weight) or weight <= 0.0:
            raise ValueError("Objective weight must be positive.")
        oid = str(obj.get("id") or f"{metric}_{idx + 1}").strip()
        if not oid:
            raise ValueError("Objective/spec id must be a non-empty string.")
        if oid in seen_ids:
            raise ValueError(f"Duplicate objective/spec id: {oid!r}. Every objective/spec must have a unique id.")
        seen_ids.add(oid)
        vds_v = float(obj["vds_v"]) if obj.get("vds_v") is not None else None
        vds_signal = obj.get("vds_signal") or obj.get("vdc_signal")
        ids_signal = obj.get("ids_signal") or obj.get("idc_signal")
        pin_dbm = float(obj["pin_dbm"]) if obj.get("pin_dbm") is not None else None
        load_ohm = float(obj.get("load_ohm", obj.get("rload_ohm", 50.0)))
        if not math.isfinite(load_ohm) or load_ohm <= 0.0:
            raise ValueError(f"Objective '{oid}' load_ohm must be positive and finite; got {load_ohm!r}.")
        at_hz = float(obj["at_hz"]) if obj.get("at_hz") is not None else None
        if at_hz is not None and (not math.isfinite(at_hz) or at_hz < 0.0):
            raise ValueError(f"Objective '{oid}' at_hz must be finite and >= 0; got {at_hz!r}.")
        norm_val = float(obj["norm"]) if obj.get("norm") is not None else None
        if norm_val is not None and (not math.isfinite(norm_val) or norm_val <= 0.0):
            raise ValueError(f"Objective '{oid}' norm must be positive and finite; got {norm_val!r}.")
        tol_val = float(obj["tolerance"]) if obj.get("tolerance") is not None else None
        if tol_val is not None and (not math.isfinite(tol_val) or tol_val < 0.0):
            raise ValueError(f"Objective '{oid}' tolerance must be finite and >= 0; got {tol_val!r}.")
        sig_name = obj.get("signal")
        # Upfront per-metric parameter validation BEFORE launching any ADS simulation
        if metric in {"s_db_at", "db20_at", "mag_at"} and at_hz is None:
            raise ValueError(f"Objective '{oid}' ({metric}) requires numeric 'at_hz'.")
        if metric in {"db20_at", "mag_at", "cutoff_3db_hz", "ripple_db", "pout_dbm", "pout_w", "h2_dbc", "thd_percent", "pae_percent", "drain_eff_percent"} and not sig_name:
            raise ValueError(f"Objective '{oid}' ({metric}) requires 'signal'.")
        if metric in {"pae_percent", "drain_eff_percent"}:
            if (vds_v is None and not vds_signal) or not ids_signal:
                raise ValueError(
                    f"Objective '{oid}' ({metric}) requires 'ids_signal' and either 'vds_signal' (recommended for adaptive bias) or 'vds_v'."
                )
            if metric == "pae_percent" and pin_dbm is None:
                raise ValueError(f"Objective '{oid}' (pae_percent) requires numeric 'pin_dbm'.")
            if target_f is not None:
                if target_f > 100.0:
                    raise ValueError(f"Objective '{oid}' ({metric}) target {target_f}% exceeds physical 100% ceiling.")
                if metric == "drain_eff_percent" and target_f < 0.0:
                    raise ValueError(f"Objective '{oid}' (drain_eff_percent) target {target_f}% cannot be negative.")
        cleaned.append({
            "id": oid,
            "metric": metric,
            "op": op,
            "target": target_f,
            "weight": weight,
            "block": obj.get("block"),
            "axis": obj.get("axis"),
            "signal": sig_name,
            "band_hz": obj.get("band_hz"),
            "at_hz": at_hz,
            "load_ohm": load_ohm,
            "return_loss_spec_db": float(obj.get("return_loss_spec_db", -10.0)),
            "vds_v": vds_v,
            "vds_signal": vds_signal,
            "ids_signal": ids_signal,
            "pin_dbm": pin_dbm,
            "filters": dict(obj["filters"]) if isinstance(obj.get("filters"), dict) and obj["filters"] else None,
            "norm": norm_val,
            "tolerance": tol_val,
        })
    return cleaned


def _to_unit_box(values: list[float], params: list[dict[str, Any]]) -> np.ndarray:
    u = np.zeros(len(params), dtype=np.float64)
    for i, (val, p) in enumerate(zip(values, params)):
        if p["scale"] == "log":
            u[i] = (math.log(val) - math.log(p["min"])) / (math.log(p["max"]) - math.log(p["min"]))
        else:
            u[i] = (val - p["min"]) / (p["max"] - p["min"])
    return np.clip(u, 0.0, 1.0)


def _from_unit_box(u: np.ndarray, params: list[dict[str, Any]]) -> list[float]:
    clamped = np.clip(np.asarray(u, dtype=np.float64), 0.0, 1.0)
    vals: list[float] = []
    for x, p in zip(clamped, params):
        if p["scale"] == "log":
            v = math.exp(math.log(p["min"]) + float(x) * (math.log(p["max"]) - math.log(p["min"])))
        else:
            v = p["min"] + float(x) * (p["max"] - p["min"])
        vals.append(v)
    return vals


def _interp_at(xs: np.ndarray, ys: np.ndarray, target_x: float) -> float:
    if len(xs) == 0 or not np.all(np.isfinite(xs)):
        raise ValueError("Axis contains missing or non-finite values.")
    idx_order = np.argsort(xs)
    sx = xs[idx_order]
    sy = ys[idx_order]
    if len(sx) >= 2 and np.any(np.diff(sx) <= 0.0):
        raise ValueError(
            "Axis contains duplicate coordinate values (likely an unfiltered multi-dimensional ParamSweep). "
            "Provide filters={sweep_var: value} or evaluate per sweep slice."
        )
    if target_x < sx[0] or target_x > sx[-1]:
        raise ValueError(f"Requested at_hz={target_x} is outside simulated axis [{sx[0]}, {sx[-1]}].")
    pos = int(np.searchsorted(sx, target_x))
    if pos < len(sx) and abs(float(sx[pos]) - target_x) <= 1e-12 * max(1.0, abs(target_x)):
        val = float(sy[pos])
        if not math.isfinite(val):
            raise ValueError(f"Missing or non-finite sample at at_hz={target_x}.")
        return val
    lo_i = max(0, pos - 1)
    hi_i = min(len(sx) - 1, pos)
    y0, y1 = float(sy[lo_i]), float(sy[hi_i])
    if not (math.isfinite(y0) and math.isfinite(y1)):
        raise ValueError(f"Missing or non-finite sample bracketing at_hz={target_x}.")
    x0, x1 = float(sx[lo_i]), float(sx[hi_i])
    if abs(x1 - x0) <= 1e-18:
        return y0
    t = (target_x - x0) / (x1 - x0)
    return float(y0 + t * (y1 - y0))


def _check_single_slice_block(raw_block: dict[str, Any], xs: np.ndarray) -> None:
    """Reject unfiltered multi-dimensional blocks BEFORE any metric branch (including db20_at / s_db_at / mag_at)."""
    block_name = str(raw_block.get("block", ""))
    axis_name = str(raw_block.get("axis", ""))
    outer_axes = raw_block.get("outer_axes") or {}
    if outer_axes:
        axes_desc = ", ".join(
            f"{k} ({len(vals)} values: {vals[:4]}{'...' if len(vals) > 4 else ''})"
            for k, vals in outer_axes.items()
        )
        raise ValueError(
            f"Block '{block_name}' is multi-dimensional with unfiltered outer sweep axis/axes: {axes_desc}. "
            "Specify filters={axis: value} or evaluate every sweep slice."
        )
    if axis_name.lower() in {"freq", "time"} and len(xs) >= 2:
        if np.any(np.diff(xs) <= 0.0):
            raise ValueError(
                f"Block '{block_name}' axis '{axis_name}' has non-monotonic or duplicate coordinates "
                "(likely an unfiltered multi-dimensional ParamSweep). Specify filters={sweep_var: value}."
            )


def _run_batched(
    batch_simulate_fn: Callable[[list[str]], dict[str, Any]],
    netlists: list[str],
    chunk_size: int = 32,
) -> list[dict[str, Any]]:
    """Chunk large netlist lists into <=chunk_size batches while preserving exact order."""
    jobs: list[dict[str, Any]] = []
    for start in range(0, len(netlists), chunk_size):
        chunk = netlists[start:start + chunk_size]
        jobs.extend(batch_simulate_fn(chunk)["jobs"])
    return jobs


def extract_objective_metric(
    raw_block: dict[str, Any],
    obj: dict[str, Any],
) -> float:
    """Compute a scalar engineering metric from an extracted ADS block using NumPy + scikit-rf."""
    axis_name = raw_block["axis"]
    xs = np.asarray(
        [math.nan if v is None else float(v) for v in raw_block["axis_values"]],
        dtype=np.float64,
    )
    if len(xs) == 0 or not np.all(np.isfinite(xs)):
        raise ValueError(f"Block '{raw_block['block']}' axis '{axis_name}' contains missing or non-finite values.")
    _check_single_slice_block(raw_block, xs)
    signals = {
        k: rf_compress.decode_complex_pairs(pairs)
        for k, pairs in raw_block["signals"].items()
    }
    metric = obj["metric"]
    sig_name = obj.get("signal")

    if metric in {"s_db_max", "s_db_min", "s_db_at", "vswr_max", "matched_bw_hz", "rollett_k_min"}:
        key = sig_name or "S[1,1]"
        if not rf_compress.is_sparam_block(raw_block["block"], axis_name, signals.keys()):
            raise ValueError(
                f"Objective '{metric}' requires an S-parameter frequency block; "
                f"got block='{raw_block['block']}', axis='{axis_name}', signals={sorted(signals)}."
            )
        if metric != "rollett_k_min" and not rf_compress.is_sparam_signal_name(key):
            raise ValueError(
                f"Objective '{metric}' requires a valid S-parameter signal name like 'S[1,1]' or 'S[2,1]'; got '{key}'."
            )
        if metric == "s_db_at":
            if obj["at_hz"] is None:
                raise ValueError("Objective 's_db_at' requires 'at_hz'.")
            if key not in signals:
                raise ValueError(f"S-parameter '{key}' not found in {sorted(signals)}.")
            mags = np.abs(signals[key])
            arr_db = np.where(np.isfinite(mags), 20.0 * np.log10(np.maximum(mags, 1e-18)), np.nan)
            return _interp_at(xs, arr_db, obj["at_hz"])
        comp = rf_compress.compress_waveform_block(
            axis_name,
            xs,
            signals,
            block_name=raw_block["block"],
            band_hz=obj.get("band_hz"),
            load_ohm=obj["load_ohm"],
            return_loss_spec_db=obj["return_loss_spec_db"],
        )
        if comp["kind"] != "sparameter":
            raise ValueError(f"Objective '{metric}' requires an S-parameter block; got '{comp['kind']}'.")
        if comp.get("missing_samples", 0) > 0:
            raise ValueError(f"S-parameter block '{raw_block['block']}' contains {comp['missing_samples']} missing/non-finite samples.")
        if metric == "rollett_k_min":
            val = comp["network_properties"].get("min_rollett_k")
            if val is None:
                raise ValueError("Rollett K requires a 2-port S-parameter network.")
            return float(val)
        if key not in comp["parameters"]:
            raise ValueError(f"S-parameter '{key}' not found in {sorted(comp['parameters'])}.")
        pinfo = comp["parameters"][key]
        if metric == "s_db_max":
            return float(pinfo["max_db"])
        if metric == "s_db_min":
            return float(pinfo["min_db"])
        if metric == "vswr_max":
            return float(pinfo["max_vswr"])
        if metric == "matched_bw_hz":
            return float(pinfo["matched_bw_hz"])

    if not sig_name or sig_name not in signals:
        raise ValueError(f"Signal '{sig_name}' not found in block '{raw_block['block']}'. Available: {sorted(signals)}")

    if metric in {"db20_at", "mag_at"}:
        if obj["at_hz"] is None:
            raise ValueError(f"Objective '{metric}' requires 'at_hz'.")
        mags = np.abs(signals[sig_name])
        if metric == "mag_at":
            return _interp_at(xs, mags, obj["at_hz"])
        arr_db = np.where(np.isfinite(mags), 20.0 * np.log10(np.maximum(mags, 1e-18)), np.nan)
        return _interp_at(xs, arr_db, obj["at_hz"])

    if not np.all(np.isfinite(signals[sig_name])):
        raise ValueError(f"Signal '{sig_name}' in block '{raw_block['block']}' contains missing/non-finite samples.")

    comp = rf_compress.compress_waveform_block(
        axis_name,
        xs,
        signals,
        block_name=raw_block["block"],
        band_hz=obj.get("band_hz"),
        load_ohm=obj["load_ohm"],
        vds_v=obj.get("vds_v"),
        vds_signal=obj.get("vds_signal"),
        ids_signal=obj.get("ids_signal"),
        vout_signal=sig_name,
        pin_dbm=obj.get("pin_dbm"),
        outer_axes=raw_block.get("outer_axes") or None,
    )
    sinfo = comp["signals"][sig_name]
    if metric in {"cutoff_3db_hz", "ripple_db"}:
        if comp["kind"] != "ac":
            raise ValueError(f"Objective '{metric}' requires an AC frequency-response block; got '{comp['kind']}'.")
        if metric == "cutoff_3db_hz":
            crossings = sinfo.get("crossings_3db_hz") or []
            if not crossings:
                raise ValueError(f"No -3 dB crossing found for '{sig_name}' in block '{raw_block['block']}'.")
            return float(crossings[0])
        return float(sinfo["ripple_db"])

    if metric in {"pout_dbm", "pout_w", "h2_dbc", "thd_percent", "pae_percent", "drain_eff_percent"}:
        if comp["kind"] != "harmonic_balance":
            raise ValueError(f"Objective '{metric}' requires a Harmonic Balance block; got '{comp['kind']}'.")
        if metric in {"pout_dbm", "pout_w"}:
            if sinfo.get("quantity") != "voltage":
                raise ValueError(
                    f"Objective '{metric}' requires a load voltage signal; '{sig_name}' is classified as '{sinfo.get('quantity')}'."
                )
            key_p = "power_dbm" if metric == "pout_dbm" else "power_w"
            val = sinfo.get(key_p)
            if val is None:
                raise ValueError(f"Objective '{metric}' unavailable for '{sig_name}' (requires voltage signal and positive load_ohm).")
            return float(val)
        if metric == "h2_dbc":
            val = sinfo.get("h2_dbc")
            if val is None:
                raise ValueError(f"H2 harmonic unavailable for '{sig_name}'.")
            return float(val)
        if metric == "thd_percent":
            val = sinfo.get("thd_percent")
            if val is None:
                raise ValueError(f"THD unavailable for '{sig_name}' (missing or insufficient harmonic samples).")
            return float(val)
        pa = comp.get("pa_metrics") or {}
        if metric not in pa or pa[metric] is None:
            raise ValueError(
                f"Objective '{metric}' requires a voltage output signal, vds_signal/vds_v, and ids_signal (plus pin_dbm for PAE)."
            )
        eff_val = float(pa[metric])
        drain_eff_val = pa.get("drain_eff_percent")
        pa_warnings = pa.get("warnings") or rf_compress.check_pa_efficiency_warnings(
            float(drain_eff_val) if drain_eff_val is not None else None,
            eff_val if metric == "pae_percent" else pa.get("pae_percent"),
            vds_source=str(pa.get("vds_source", "constant")),
            vds_v=pa.get("vds_v"),
        )
        if pa.get("physically_valid") is False or pa_warnings or not math.isfinite(eff_val) or eff_val > rf_compress.MAX_PHYSICAL_EFFICIENCY_PERCENT:
            detail = "; ".join(pa_warnings) if pa_warnings else f"{metric}={eff_val:.2f}% exceeds 100%."
            raise ValueError(f"Unphysical efficiency measurement rejected for '{metric}': {detail}")
        return eff_val
    raise ValueError(f"Unhandled metric: {metric}")


def _evaluate_cost(measured: dict[str, float], objectives: list[dict[str, Any]]) -> tuple[float, dict[str, dict[str, Any]], bool]:
    total = 0.0
    details: dict[str, dict[str, Any]] = {}
    all_sat = True
    for obj in objectives:
        oid = obj["id"]
        val = measured[oid]
        op = obj["op"]
        target = obj["target"]
        w = obj["weight"]
        explicit_norm = obj.get("norm")
        explicit_tol = obj.get("tolerance")
        if explicit_norm is not None:
            norm = float(explicit_norm)
        elif explicit_tol is not None and explicit_tol > 0.0:
            norm = float(explicit_tol)
        elif target is not None and abs(target) > 0.0:
            norm = abs(float(target))
        else:
            norm = 1.0

        physical_violation = False
        if not math.isfinite(val):
            sat = False
            cost = w * 1e9
        elif obj["metric"] in {"pae_percent", "drain_eff_percent"} and (
            val > rf_compress.MAX_PHYSICAL_EFFICIENCY_PERCENT
            or (obj["metric"] == "drain_eff_percent" and val < 0.0)
        ):
            physical_violation = True
            sat = False
            excess_eff = abs(val - 100.0)
            cost = w * (1e6 + excess_eff * excess_eff)
        elif op == "eq":
            err = (val - target) / norm
            cost = w * (err * err)
            if explicit_tol is not None:
                eq_tol = float(explicit_tol)
            elif explicit_norm is not None:
                eq_tol = 0.02 * float(explicit_norm)
            elif abs(target) > 0.0:
                eq_tol = 0.02 * abs(float(target))
            else:
                eq_tol = 1e-9 if obj["metric"] in _LINEAR_ZERO_METRICS else 0.02
            sat = abs(val - target) <= eq_tol + 1e-12 * max(abs(float(target)), eq_tol, 1e-18)
        elif op == "le":
            excess = max(0.0, (val - target) / norm)
            cost = w * (excess * excess)
            eps = 1e-12 * max(abs(float(target)), 1e-18)
            sat = val <= target + eps
        elif op == "ge":
            deficit = max(0.0, (target - val) / norm)
            cost = w * (deficit * deficit)
            eps = 1e-12 * max(abs(float(target)), 1e-18)
            sat = val >= target - eps
        elif op == "min":
            cost = w * (val / norm)
            sat = True
        else:  # "max"
            cost = -w * (val / norm)
            sat = True

        if not sat:
            all_sat = False
        total += cost
        entry: dict[str, Any] = {
            "metric": obj["metric"],
            "op": op,
            "target": target,
            "measured": _display_float(val, decimals=6, sig_digits=6),
            "satisfied": sat,
            "cost_contrib": _display_float(cost, decimals=6, sig_digits=6),
        }
        if physical_violation:
            entry["physical_violation"] = True
        details[oid] = entry
    return float(total), details, all_sat


def _fallback_nelder_mead(
    fn: Callable[[np.ndarray], float],
    x0: np.ndarray,
    max_evals: int,
    tol: float,
) -> tuple[np.ndarray, float]:
    """Pure-NumPy Nelder-Mead fallback when SciPy is unavailable."""
    n = len(x0)
    simplex = [np.clip(x0.copy(), 0.0, 1.0)]
    for i in range(n):
        pt = x0.copy()
        pt[i] = pt[i] + 0.15 if pt[i] <= 0.8 else pt[i] - 0.15
        simplex.append(np.clip(pt, 0.0, 1.0))
    fvals = [fn(pt) for pt in simplex]
    evals = len(simplex)

    while evals < max_evals:
        order = np.argsort(fvals)
        simplex = [simplex[i] for i in order]
        fvals = [fvals[i] for i in order]
        if abs(fvals[-1] - fvals[0]) < tol:
            break
        centroid = np.mean(simplex[:-1], axis=0)
        xr = np.clip(centroid + (centroid - simplex[-1]), 0.0, 1.0)
        fr = fn(xr)
        evals += 1
        if fvals[0] <= fr < fvals[-2]:
            simplex[-1], fvals[-1] = xr, fr
        elif fr < fvals[0]:
            xe = np.clip(centroid + 2.0 * (xr - centroid), 0.0, 1.0)
            fe = fn(xe)
            evals += 1
            if fe < fr:
                simplex[-1], fvals[-1] = xe, fe
            else:
                simplex[-1], fvals[-1] = xr, fr
        else:
            xc = np.clip(centroid + 0.5 * (simplex[-1] - centroid), 0.0, 1.0)
            fc = fn(xc)
            evals += 1
            if fc < fvals[-1]:
                simplex[-1], fvals[-1] = xc, fc
            else:
                for i in range(1, len(simplex)):
                    simplex[i] = np.clip(simplex[0] + 0.5 * (simplex[i] - simplex[0]), 0.0, 1.0)
                    fvals[i] = fn(simplex[i])
                    evals += 1
                    if evals >= max_evals:
                        break
    best_i = int(np.argmin(fvals))
    return simplex[best_i], fvals[best_i]


def optimize_netlist(
    netlist_text: str,
    parameters: list[dict[str, Any]],
    objectives: list[dict[str, Any]],
    *,
    simulate_fn: Callable[[str], dict[str, Any]],
    extract_block_fn: Callable[[str, str | None, str | None], dict[str, Any]],
    batch_simulate_fn: Callable[[list[str]], dict[str, Any]] | None = None,
    method: str = "nelder_mead",
    max_evals: int = 30,
    tol: float = 1e-4,
) -> dict[str, Any]:
    """Run closed-loop derivative-free optimization on an ADS netlist and return compact JSON."""
    if method not in SUPPORTED_METHODS:
        raise ValueError(f"Unsupported optimization method {method!r}. Supported: {sorted(SUPPORTED_METHODS)}")
    if not isinstance(max_evals, int) or not 3 <= max_evals <= 120:
        raise ValueError("max_evals must be an integer between 3 and 120.")

    params = _validate_parameters(parameters)
    objs = _validate_objectives(objectives)

    # Pre-validate that all parameter refs exist in the input netlist before launching any simulation
    init_vals = [p["init"] for p in params]
    init_map = {p["ref"]: f"{v:.8g} {p['unit']}".strip() for p, v in zip(params, init_vals)}
    netlist_parser.apply_netlist_parameters(netlist_text, init_map)

    t0 = time.monotonic()
    eval_count = 0
    failed_evals = 0
    last_error: str | None = None
    cache: dict[tuple[float, ...], float] = {}
    milestones: list[dict[str, Any]] = []
    best_state: dict[str, Any] = {
        "cost": float("inf"),
        "params": None,
        "metrics": None,
        "satisfied": False,
        "job_id": None,
        "netlist": None,
    }
    initial_cost: float | None = None

    def _evaluate_job_result(vals: list[float], job: dict[str, Any], patched_nl: str) -> float:
        nonlocal failed_evals, initial_cost, last_error
        if job.get("state") != "succeeded":
            failed_evals += 1
            last_error = f"sim_{job.get('state')}: {job.get('code')} {job.get('message', '')}".strip()
            return 1e9
        job_id = job["job_id"]
        block_cache: dict[tuple[str | None, str | None], dict[str, Any]] = {}
        measured: dict[str, float] = {}
        try:
            for obj in objs:
                bkey = (obj.get("block"), obj.get("axis"))
                if bkey not in block_cache:
                    block_cache[bkey] = extract_block_fn(job_id, obj.get("block"), obj.get("axis"))
                measured[obj["id"]] = extract_objective_metric(block_cache[bkey], obj)
        except Exception as exc:
            failed_evals += 1
            last_error = f"{type(exc).__name__}: {exc}"
            return 1e9

        cost, details, all_sat = _evaluate_cost(measured, objs)
        if initial_cost is None:
            initial_cost = cost
        if cost < best_state["cost"]:
            best_state.update(
                cost=cost,
                params=list(vals),
                metrics=details,
                satisfied=all_sat,
                job_id=job_id,
                netlist=patched_nl,
            )
            milestones.append({
                "eval": eval_count,
                "cost": round(cost, 6),
                "satisfied": all_sat,
                "params": {p["ref"]: _sig_float(v, 8) for p, v in zip(params, vals)},
            })
        return cost

    def _objective_u(u: np.ndarray) -> float:
        nonlocal eval_count
        u_clamped = np.clip(np.asarray(u, dtype=np.float64), 0.0, 1.0)
        key = tuple(round(float(x), 6) for x in u_clamped)
        if key in cache:
            return cache[key]
        if eval_count >= max_evals:
            return best_state["cost"] + 1e3
        eval_count += 1
        vals = _from_unit_box(u_clamped, params)
        updates = {p["ref"]: f"{v:.8g} {p['unit']}".strip() for p, v in zip(params, vals)}
        patched = netlist_parser.apply_netlist_parameters(netlist_text, updates)
        job = simulate_fn(patched)
        cost = _evaluate_job_result(vals, job, patched)
        cache[key] = cost
        return cost

    u0 = _to_unit_box(init_vals, params)

    # Optional parallel coarse grid screen before local Nelder-Mead polish
    if method == "grid_refine":
        levels = 3 if len(params) <= 2 else 2
        grid_1d = np.linspace(0.1, 0.9, levels)
        raw_candidates = [np.array(pt, dtype=np.float64) for pt in itertools.product(grid_1d, repeat=len(params))]
        raw_candidates.insert(0, u0)
        budget_grid = max(2, min(len(raw_candidates), max_evals // 2))
        chosen_grid = raw_candidates[:budget_grid]
        if batch_simulate_fn is not None:
            nl_batch = []
            val_batch = []
            for ug in chosen_grid:
                vals = _from_unit_box(ug, params)
                updates = {p["ref"]: f"{v:.8g} {p['unit']}".strip() for p, v in zip(params, vals)}
                nl_batch.append(netlist_parser.apply_netlist_parameters(netlist_text, updates))
                val_batch.append(vals)
            batch_jobs = _run_batched(batch_simulate_fn, nl_batch)
            for ug, vals, nl_item, job in zip(chosen_grid, val_batch, nl_batch, batch_jobs):
                eval_count += 1
                key = tuple(round(float(x), 6) for x in np.clip(ug, 0.0, 1.0))
                cache[key] = _evaluate_job_result(vals, job, nl_item)
        else:
            for ug in chosen_grid:
                _objective_u(ug)
        if best_state["params"] is not None:
            u0 = _to_unit_box(best_state["params"], params)

    remaining = max(0, max_evals - eval_count)
    if remaining > 0:
        if scipy_minimize is not None:
            if method == "powell":
                scipy_minimize(
                    _objective_u,
                    u0,
                    method="Powell",
                    bounds=[(0.0, 1.0)] * len(params),
                    options={"maxfev": remaining, "ftol": tol, "xtol": tol},
                )
            else:
                scipy_minimize(
                    _objective_u,
                    u0,
                    method="Nelder-Mead",
                    options={"maxfev": remaining, "fatol": tol, "xatol": tol, "adaptive": True},
                )
        else:
            _fallback_nelder_mead(_objective_u, u0, remaining, tol)

    elapsed = round(time.monotonic() - t0, 3)
    if best_state["params"] is None:
        return {
            "ok": False,
            "converged": False,
            "method": method,
            "evaluations": eval_count,
            "failed_evaluations": failed_evals,
            "elapsed_s": elapsed,
            "last_error": last_error,
            "message": f"All simulation evaluations failed during optimization. Last error: {last_error}",
        }

    best_params_out = {
        p["ref"]: {
            "value": _sig_float(v, 10),
            "formatted": f"{v:.6g} {p['unit']}".strip(),
            "unit": p["unit"],
        }
        for p, v in zip(params, best_state["params"])
    }
    return {
        "schema": "ads_bridge.optimize_result.v1",
        "ok": True,
        "all_satisfied": bool(best_state["satisfied"]),
        "all_pass": bool(best_state["satisfied"]),
        "method": method,
        "engine": {
            **rf_compress.engine_versions(),
            "scipy": "installed" if scipy_minimize is not None else "fallback_nelder_mead",
        },
        "evaluations": eval_count,
        "failed_evaluations": failed_evals,
        "elapsed_s": elapsed,
        "initial_cost": round(initial_cost, 6) if initial_cost is not None else None,
        "best_cost": round(best_state["cost"], 6),
        "best_job_id": best_state["job_id"],
        "best_parameters": best_params_out,
        "best_metrics": best_state["metrics"],
        "best_netlist": best_state["netlist"],
        "milestones": milestones[:12],
    }


def _call_extract_block(
    extract_block_fn: Callable[..., dict[str, Any]],
    block: str | None,
    axis: str | None,
    filters: dict[str, Any] | None,
) -> dict[str, Any]:
    if filters:
        try:
            return extract_block_fn(block, axis, filters)
        except TypeError:
            pass
    return extract_block_fn(block, axis)


def evaluate_specs_scorecard(
    objectives: list[dict[str, Any]],
    extract_block_fn: Callable[..., dict[str, Any]],
    *,
    extract_sweep_fn: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Evaluate a declarative list of RF/circuit specs and return a compact tabular Scorecard.

    For 2D ParamSweep blocks without explicit `filters`, evaluates EVERY slice along the outer sweep axis
    when `extract_sweep_fn` is provided (all slices must pass for `all_pass=True`); otherwise rejects
    unfiltered multi-dimensional blocks instead of silently picking one arbitrary slice.
    """
    objs = _validate_objectives(objectives)
    block_cache: dict[tuple[str | None, str | None, str], dict[str, Any]] = {}
    sweep_cache: dict[tuple[str, str | None, str | None, str], dict[str, Any]] = {}

    # Reserve all explicit user spec IDs upfront so auto-generated slice row IDs can NEVER collide with them.
    reserved_user_ids: set[str] = {o["id"] for o in objs}
    used_row_ids: set[str] = set()

    def _allocate_slice_id(base_spec_id: str, sw_axis: str, sv: Any, sl_idx: int) -> str:
        sv_str = f"{sv:.12g}" if isinstance(sv, (int, float)) else str(sv)
        cand = f"{base_spec_id}[{sw_axis}={sv_str}]"
        if cand not in reserved_user_ids and cand not in used_row_ids:
            used_row_ids.add(cand)
            return cand
        # Disambiguate collisions (e.g. >12-digit close sweep values or manual spec ID matching auto pattern)
        suffix = sl_idx
        while True:
            alt = f"{cand}#{suffix}"
            if alt not in reserved_user_ids and alt not in used_row_ids:
                used_row_ids.add(alt)
                return alt
            suffix += 1

    # Evaluate every row positionally (never via a shared dict that could overwrite on duplicate keys)
    evaluated_rows: list[tuple[dict[str, Any], dict[str, Any]]] = []
    total_cost = 0.0
    all_sat = True
    sweep_summary: list[dict[str, Any]] = []

    for obj in objs:
        flt = obj.get("filters")
        flt_key = json.dumps(flt, sort_keys=True) if flt else ""
        bkey = (obj.get("block"), obj.get("axis"), flt_key)
        if bkey not in block_cache:
            block_cache[bkey] = _call_extract_block(extract_block_fn, obj.get("block"), obj.get("axis"), flt)
        raw_blk = block_cache[bkey]
        outer_axes = raw_blk.get("outer_axes") or {}

        if outer_axes and len(outer_axes) == 1 and extract_sweep_fn is not None:
            sw_axis = next(iter(outer_axes.keys()))
            skey = (sw_axis, raw_blk.get("block"), raw_blk.get("axis"), flt_key)
            if skey not in sweep_cache:
                sweep_cache[skey] = extract_sweep_fn(sw_axis, raw_blk.get("block"), raw_blk.get("axis"), flt)
            sw_data = sweep_cache[skey]
            slices = sw_data.get("slices") or []
            if not slices:
                raise ValueError(f"Swept block '{raw_blk.get('block')}' produced 0 slices along '{sw_axis}'.")
            slice_pass_count = 0
            slice_fail_items: list[dict[str, Any]] = []
            for sl_idx, sl in enumerate(slices):
                sv = sl["sweep_value"]
                sl_id = _allocate_slice_id(obj["id"], sw_axis, sv, sl_idx)
                sl_obj = {**obj, "id": sl_id}
                sl_block = {
                    "block": sw_data["block"],
                    "axis": sw_data["axis"],
                    "axis_values": sl["axis_values"],
                    "signals": sl["signals"],
                    "outer_axes": {},
                }
                val = extract_objective_metric(sl_block, sl_obj)
                row_cost, det_one, sat_one = _evaluate_cost({sl_id: val}, [sl_obj])
                row_detail = det_one[sl_id]
                evaluated_rows.append((sl_obj, row_detail))
                total_cost += row_cost
                if sat_one:
                    slice_pass_count += 1
                else:
                    all_sat = False
                    slice_fail_items.append({sw_axis: sv, "measured": row_detail["measured"]})
            sweep_summary.append({
                "spec": obj["id"],
                "sweep_axis": sw_axis,
                "total_slices": len(slices),
                "passed_slices": slice_pass_count,
                "failed_slices": len(slices) - slice_pass_count,
                "failing_samples": slice_fail_items[:8],
            })
        else:
            used_row_ids.add(obj["id"])
            val = extract_objective_metric(raw_blk, obj)
            row_cost, det_one, sat_one = _evaluate_cost({obj["id"]: val}, [obj])
            evaluated_rows.append((obj, det_one[obj["id"]]))
            total_cost += row_cost
            if not sat_one:
                all_sat = False

    if any(s["failed_slices"] > 0 for s in sweep_summary):
        all_sat = False

    op_sym = {"eq": "==", "le": "<=", "ge": ">=", "min": "min", "max": "max"}
    scorecard: list[list[Any]] = []
    details: dict[str, dict[str, Any]] = {}
    for obj, d in evaluated_rows:
        oid = obj["id"]
        details[oid] = d
        val = d["measured"]
        tgt = obj["target"]
        op = obj["op"]
        if tgt is not None and val is not None:
            if op == "le":
                margin = _display_float(tgt - val, decimals=4, sig_digits=5)  # positive = passing margin
            elif op == "ge":
                margin = _display_float(val - tgt, decimals=4, sig_digits=5)
            else:
                margin = _display_float(abs(val - tgt), decimals=4, sig_digits=5)
            target_str = f"{op_sym[op]} {tgt:.12g}"
        else:
            margin = None
            target_str = op_sym[op]
        scorecard.append([oid, val, target_str, "PASS" if d["satisfied"] else "FAIL", margin])

    out: dict[str, Any] = {
        "all_pass": bool(all_sat),
        "total_cost": round(total_cost, 6),
        "scorecard_cols": ["spec", "measured", "target", "status", "margin"],
        "scorecard": scorecard,
        "metrics": details,
    }
    if sweep_summary:
        out["sweep_evaluation"] = sweep_summary
    return out


def sensitivity_scan(
    netlist_text: str,
    parameters: list[str | dict[str, Any]],
    objectives: list[dict[str, Any]],
    *,
    simulate_fn: Callable[[str], dict[str, Any]],
    extract_block_fn: Callable[[str, str | None, str | None], dict[str, Any]],
    batch_simulate_fn: Callable[[list[str]], dict[str, Any]] | None = None,
    step_pct: float = 5.0,
) -> dict[str, Any]:
    """Run local +/-step_pct perturbation sensitivity ranking inside the bridge layer."""
    if not isinstance(parameters, list) or not 1 <= len(parameters) <= 16:
        raise ValueError("Provide between 1 and 16 parameters for sensitivity_scan.")
    if not math.isfinite(step_pct) or not 0.1 <= step_pct <= 50.0:
        raise ValueError("step_pct must be between 0.1 and 50.0.")

    objs = _validate_objectives(objectives)
    compact = netlist_parser.compress_netlist(netlist_text, include_models=False, include_subcircuits=False)
    tunable_map = {tp["ref"]: tp for tp in compact.get("tunable_params", ())}

    resolved_params: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in parameters:
        if isinstance(item, str):
            ref = item.strip()
            p_step = float(step_pct)
            val_override = None
            unit_override = None
        elif isinstance(item, dict) and isinstance(item.get("ref"), str) and item["ref"].strip():
            ref = item["ref"].strip()
            p_step = float(item.get("step_pct", step_pct))
            val_override = item.get("nominal")
            unit_override = item.get("unit")
        else:
            raise ValueError("Each sensitivity parameter must be a ref string or {'ref': ..., 'step_pct': ...}.")
        if ref in seen:
            raise ValueError(f"Duplicate sensitivity parameter ref: {ref}")
        seen.add(ref)
        if not 0.1 <= p_step <= 50.0:
            raise ValueError(f"Parameter '{ref}' step_pct must be between 0.1 and 50.0.")
        tp = tunable_map.get(ref)
        if val_override is not None:
            nom = float(val_override)
            unit = str(unit_override if unit_override is not None else (tp["unit"] if tp else "")).strip()
        elif tp is not None:
            raw_str = tp["raw"].strip()
            m_num = netlist_parser._NUMBER_UNIT_RE.match(raw_str)
            if m_num:
                nom = float(m_num.group(1))
                unit = m_num.group(2).strip()
            else:
                nom = float(tp["si"])
                unit = ""
        else:
            raise ValueError(f"Could not resolve numeric nominal value for parameter '{ref}'. Provide 'nominal' explicitly.")
        if not math.isfinite(nom) or nom == 0.0:
            raise ValueError(f"Sensitivity parameter '{ref}' requires a finite non-zero nominal value (got {nom}).")
        resolved_params.append({"ref": ref, "nominal": nom, "unit": unit, "step_pct": p_step})

    # Build baseline + (+step, -step) netlists for every parameter
    t0 = time.monotonic()
    netlists: list[str] = [netlist_text]
    for p in resolved_params:
        frac = p["step_pct"] / 100.0
        v_hi = p["nominal"] * (1.0 + frac)
        v_lo = p["nominal"] * (1.0 - frac)
        netlists.append(netlist_parser.apply_netlist_parameters(netlist_text, {p["ref"]: f"{v_hi:.8g} {p['unit']}".strip()}))
        netlists.append(netlist_parser.apply_netlist_parameters(netlist_text, {p["ref"]: f"{v_lo:.8g} {p['unit']}".strip()}))

    if batch_simulate_fn is not None and len(netlists) > 1:
        jobs = _run_batched(batch_simulate_fn, netlists)
    else:
        jobs = [simulate_fn(nl) for nl in netlists]

    def _measure_job(job: dict[str, Any]) -> dict[str, float] | None:
        if job.get("state") != "succeeded":
            return None
        jid = job["job_id"]
        bcache: dict[tuple[str | None, str | None], dict[str, Any]] = {}
        out_m: dict[str, float] = {}
        try:
            for obj in objs:
                bkey = (obj.get("block"), obj.get("axis"))
                if bkey not in bcache:
                    bcache[bkey] = extract_block_fn(jid, obj.get("block"), obj.get("axis"))
                out_m[obj["id"]] = extract_objective_metric(bcache[bkey], obj)
        except Exception:
            return None
        return out_m

    base_metrics = _measure_job(jobs[0])
    if base_metrics is None:
        return {
            "schema": "ads_bridge.sensitivity.v1",
            "ok": False,
            "elapsed_s": round(time.monotonic() - t0, 3),
            "message": "Baseline simulation or metric extraction failed.",
        }

    ranked: list[dict[str, Any]] = []
    for idx, p in enumerate(resolved_params):
        m_hi = _measure_job(jobs[1 + 2 * idx])
        m_lo = _measure_job(jobs[2 + 2 * idx])
        frac = p["step_pct"] / 100.0
        per_obj: dict[str, Any] = {}
        agg_score = 0.0
        for obj in objs:
            oid = obj["id"]
            bval = base_metrics[oid]
            norm = obj["norm"] if obj["norm"] is not None else max(abs(obj["target"] or bval), 1.0)
            if m_hi is not None and m_lo is not None:
                delta = m_hi[oid] - m_lo[oid]
                d_per_1pct = delta / (2.0 * p["step_pct"])
                norm_impact = abs(delta / (2.0 * frac)) / norm
            elif m_hi is not None:
                delta = m_hi[oid] - bval
                d_per_1pct = delta / p["step_pct"]
                norm_impact = abs(delta / frac) / norm
            elif m_lo is not None:
                delta = bval - m_lo[oid]
                d_per_1pct = delta / p["step_pct"]
                norm_impact = abs(delta / frac) / norm
            else:
                d_per_1pct = None
                norm_impact = 0.0
            agg_score += obj["weight"] * norm_impact
            per_obj[oid] = {
                "delta_per_1pct": round(d_per_1pct, 6) if d_per_1pct is not None else None,
                "norm_impact": round(norm_impact, 4),
            }
        ranked.append({
            "ref": p["ref"],
            "nominal": f"{p['nominal']:.6g} {p['unit']}".strip(),
            "step_pct": p["step_pct"],
            "impact_score": round(agg_score, 4),
            "effects": per_obj,
        })

    ranked.sort(key=lambda r: r["impact_score"], reverse=True)
    for rank_i, row in enumerate(ranked, start=1):
        row["rank"] = rank_i

    return {
        "schema": "ads_bridge.sensitivity.v1",
        "ok": True,
        "evaluations": len(jobs),
        "elapsed_s": round(time.monotonic() - t0, 3),
        "baseline": {k: round(v, 6) for k, v in base_metrics.items()},
        "ranked_params": ranked,
    }
