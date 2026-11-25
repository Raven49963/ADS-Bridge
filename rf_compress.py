"""RF & circuit simulation data compression using Python + NumPy + scikit-rf.

Extracts compact, LLM-ready engineering JSON from high-resolution Keysight ADS
datasets (S-parameters, AC sweeps, Harmonic Balance spectra, Transient waveforms)
and Touchstone (.sNp) files so AI models never ingest thousands of raw samples.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
import re
from typing import Any

import numpy as np

try:
    import skrf  # type: ignore
except ImportError:
    skrf = None  # type: ignore


_SPARAM_RE = re.compile(r"^S\[(\d+)\s*,\s*(\d+)\]$")
_CURRENT_SIGNAL_RE = re.compile(r"^(?:i[_\.\[]|ids|idc|iin|iout|idrain|isrc|iload|ibias)", re.IGNORECASE)


def engine_versions() -> dict[str, str | None]:
    return {
        "numpy": getattr(np, "__version__", None),
        "scikit_rf": getattr(skrf, "__version__", None) if skrf is not None else None,
    }


def decode_complex_pairs(pairs: list[list[float | None]] | list[tuple[float | None, float | None]]) -> np.ndarray:
    """Decode JSON `[re, im]` pairs into complex128, preserving missing/non-finite entries as `nan+nanj`."""
    out = np.empty(len(pairs), dtype=np.complex128)
    for idx, pair in enumerate(pairs):
        r, im = pair[0], pair[1]
        if r is None or im is None:
            out[idx] = complex(math.nan, math.nan)
        else:
            rf, imf = float(r), float(im)
            if math.isfinite(rf) and math.isfinite(imf):
                out[idx] = complex(rf, imf)
            else:
                out[idx] = complex(math.nan, math.nan)
    return out


def is_sparam_signal_name(name: str) -> bool:
    """Return True if *name* is a canonical ADS S-parameter identifier like `S[1,1]`."""
    return isinstance(name, str) and bool(_SPARAM_RE.match(name.strip()))


def is_sparam_block(block_name: str, axis_name: str, signal_names: Any) -> bool:
    """Shared S-parameter block identity check used by both compression and objective scoring."""
    if axis_name.strip().lower() != "freq":
        return False
    low_block = block_name.strip().lower()
    if (
        ".ac" in low_block or low_block == "ac" or low_block.endswith("_ac")
        or ".hb" in low_block or low_block == "hb" or low_block.endswith("_hb")
        or ".tran" in low_block or low_block == "tran"
        or ".dc" in low_block or low_block == "dc"
    ):
        return False
    return any(is_sparam_signal_name(str(n)) for n in signal_names)


def _is_current_signal(name: str, ids_signal: str | None = None, vout_signal: str | None = None) -> bool:
    """Classify current probes uniformly across compression and scoring (never overridden by vout_signal)."""
    _ = vout_signal  # Intentionally never overrides explicit current probe identity
    if ids_signal and name == ids_signal:
        return True
    low = name.strip().lower()
    if low.endswith(".i") or ".i[" in low:
        return True
    return bool(_CURRENT_SIGNAL_RE.match(low))


def _resolve_band_mask(xs: np.ndarray, band_hz: tuple[float, float] | list[float] | None) -> np.ndarray:
    if band_hz is None:
        return np.ones(len(xs), dtype=bool)
    if len(band_hz) != 2:
        raise ValueError(f"band_hz must be a 2-element [start_hz, stop_hz] sequence; got {band_hz!r}")
    f_lo, f_hi = float(band_hz[0]), float(band_hz[1])
    if not (math.isfinite(f_lo) and math.isfinite(f_hi) and f_hi >= f_lo):
        raise ValueError(f"Invalid band_hz range: {band_hz!r}")
    mask = np.isfinite(xs) & (xs >= f_lo) & (xs <= f_hi)
    if not np.any(mask):
        finite_x = xs[np.isfinite(xs)]
        span = f"[{float(np.min(finite_x))}, {float(np.max(finite_x))}]" if finite_x.size else "[]"
        raise ValueError(f"Requested band_hz=[{f_lo}, {f_hi}] contains 0 samples within simulated range {span}.")
    return mask


def _finite(val: Any) -> float | None:
    try:
        f = float(val)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _db20_arr(mag: np.ndarray) -> np.ndarray:
    safe = np.maximum(mag, 1e-18)
    return 20.0 * np.log10(safe)


def lttb_downsample_indices(xs: np.ndarray, ys: np.ndarray, max_points: int = 24) -> list[int]:
    """Largest-Triangle-Three-Buckets (LTTB) + global extrema-preserving index selector."""
    n = len(xs)
    if n <= max_points or max_points < 3:
        return list(range(n))

    # Sanitize non-finite values so triangle area computation never hits inf - inf -> NaN
    finite_mask = np.isfinite(ys)
    if np.any(finite_mask):
        y_lo = float(np.min(ys[finite_mask]))
        y_hi = float(np.max(ys[finite_mask]))
        ys_safe = np.where(finite_mask, ys, np.where(ys < 0, y_lo, y_hi))
    else:
        ys_safe = np.zeros_like(ys, dtype=np.float64)

    # Reserve slots for endpoints + global min/max so critical resonances/notches are never lost
    selected: list[int] = [0]
    bucket_count = max_points - 2
    bucket_size = (n - 2) / bucket_count

    prev_idx = 0
    for i in range(bucket_count):
        start = int(math.floor(i * bucket_size)) + 1
        end = int(math.floor((i + 1) * bucket_size)) + 1
        end = min(end, n - 1)
        if start >= end:
            continue

        next_start = int(math.floor((i + 1) * bucket_size)) + 1
        next_end = int(math.floor((i + 2) * bucket_size)) + 1
        next_end = min(next_end, n)
        if next_start < next_end:
            avg_x = float(np.mean(xs[next_start:next_end]))
            avg_y = float(np.mean(ys_safe[next_start:next_end]))
        else:
            avg_x = float(xs[-1])
            avg_y = float(ys_safe[-1])

        ax = float(xs[prev_idx])
        ay = float(ys_safe[prev_idx])
        cand_x = xs[start:end]
        cand_y = ys_safe[start:end]
        areas = np.abs((ax - avg_x) * (cand_y - ay) - (ax - cand_x) * (avg_y - ay))
        best_local = int(np.argmax(areas)) + start
        selected.append(best_local)
        prev_idx = best_local

    selected.append(n - 1)
    idx_set = set(selected)
    # Guarantee exact global min and max indices are always included
    if np.any(finite_mask):
        valid_indices = np.flatnonzero(finite_mask)
        idx_set.add(int(valid_indices[int(np.argmin(ys[valid_indices]))]))
        idx_set.add(int(valid_indices[int(np.argmax(ys[valid_indices]))]))
    return sorted(idx_set)


def _find_contiguous_bands(freqs: np.ndarray, mask: np.ndarray) -> list[dict[str, float]]:
    bands: list[dict[str, float]] = []
    n = len(freqs)
    i = 0
    while i < n:
        if not mask[i]:
            i += 1
            continue
        start = i
        while i + 1 < n and mask[i + 1]:
            i += 1
        end = i
        f_lo = float(freqs[start])
        f_hi = float(freqs[end])
        bands.append({
            "start_hz": f_lo,
            "stop_hz": f_hi,
            "bw_hz": f_hi - f_lo,
        })
        i += 1
    return bands


def _estimate_3db_crossings_np(xs: np.ndarray, mags: np.ndarray) -> list[float]:
    if len(xs) < 2:
        return []
    peak = float(np.max(mags))
    if not math.isfinite(peak) or peak <= 0.0:
        return []
    target = peak / math.sqrt(2.0)
    crossings: list[float] = []
    for i in range(len(xs) - 1):
        m0, m1 = float(mags[i]), float(mags[i + 1])
        if (m0 - target) * (m1 - target) <= 0.0 and abs(m1 - m0) > 1e-18:
            t = (target - m0) / (m1 - m0)
            x0, x1 = float(xs[i]), float(xs[i + 1])
            if x0 > 0 and x1 > 0 and (x1 / x0 > 1.05 or x0 / x1 > 1.05):
                xc = math.exp(math.log(x0) + t * (math.log(x1) - math.log(x0)))
            else:
                xc = x0 + t * (x1 - x0)
            crossings.append(xc)
    return crossings


def _extract_sparam_matrix(signals: dict[str, np.ndarray]) -> tuple[int, np.ndarray] | None:
    entries: dict[tuple[int, int], np.ndarray] = {}
    max_port = 0
    for name, arr in signals.items():
        m = _SPARAM_RE.match(name.strip())
        if m:
            i, j = int(m.group(1)), int(m.group(2))
            if i >= 1 and j >= 1:
                entries[(i, j)] = arr
                max_port = max(max_port, i, j)
    if max_port == 0:
        return None
    if len(entries) < max_port * max_port:
        return None
    n_pts = len(next(iter(entries.values())))
    s_mat = np.zeros((n_pts, max_port, max_port), dtype=np.complex128)
    for (i, j), arr in entries.items():
        s_mat[:, i - 1, j - 1] = arr
    return max_port, s_mat


def compress_sparam_network(
    freqs_hz: np.ndarray | list[float],
    s_matrix: np.ndarray,
    *,
    z0_ohm: float = 50.0,
    max_curve_points: int = 24,
    band_hz: tuple[float, float] | list[float] | None = None,
    return_loss_spec_db: float = -10.0,
    touchstone_path: str | Path | None = None,
    name: str = "ads_network",
) -> dict[str, Any]:
    """Compress N-port S-parameter frequency sweep into compact RF engineering JSON via scikit-rf + numpy."""
    freqs = np.asarray(freqs_hz, dtype=np.float64)
    s_arr = np.asarray(s_matrix, dtype=np.complex128)
    if s_arr.ndim != 3 or s_arr.shape[0] != len(freqs) or s_arr.shape[1] != s_arr.shape[2]:
        raise ValueError(f"Expected S-matrix shape (n_freq, n_ports, n_ports); got {s_arr.shape}")

    n_freq, n_ports, _ = s_arr.shape
    raw_complex_count = int(n_freq * n_ports * n_ports)
    missing_samples = int(np.sum(~np.isfinite(s_arr.real) | ~np.isfinite(s_arr.imag)))

    band_mask = _resolve_band_mask(freqs, band_hz)
    finite_freq_mask = np.isfinite(freqs) & np.all(np.isfinite(s_arr.real) & np.isfinite(s_arr.imag), axis=(1, 2))
    active_band_mask = band_mask & finite_freq_mask
    if not np.any(active_band_mask):
        raise ValueError("All S-parameter samples in the requested band are missing or non-finite.")

    ntwk = None
    exported_touchstone: str | None = None
    if skrf is not None and n_freq >= 1 and missing_samples == 0:
        freq_obj = skrf.Frequency.from_f(freqs, unit="Hz")
        ntwk = skrf.Network(frequency=freq_obj, s=s_arr, z0=z0_ohm, name=name)
        if touchstone_path is not None:
            out_p = Path(touchstone_path)
            out_p.parent.mkdir(parents=True, exist_ok=True)
            ntwk.write_touchstone(filename=out_p.stem, dir=str(out_p.parent), write_z0=True)
            exported_touchstone = str(out_p.parent / f"{out_p.stem}.s{n_ports}p")

    with np.errstate(divide="ignore", invalid="ignore"):
        mags_all = np.abs(s_arr)
        fallback_db = np.where(np.isfinite(mags_all), _db20_arr(np.where(np.isfinite(mags_all), mags_all, 1.0)), np.nan)
        raw_s_db = ntwk.s_db if ntwk is not None else fallback_db
        s_db = np.where(np.isfinite(mags_all), np.where(np.isfinite(raw_s_db), raw_s_db, fallback_db), np.nan)
        s_deg = ntwk.s_deg if ntwk is not None else np.where(np.isfinite(mags_all), np.degrees(np.angle(s_arr)), np.nan)
        vswr = ntwk.s_vswr if ntwk is not None else np.where(
            np.isfinite(mags_all),
            (1.0 + mags_all) / np.maximum(1.0 - mags_all, 1e-12),
            np.nan,
        )
        z_mat = ntwk.z if ntwk is not None else None

    s_eval = s_arr[active_band_mask]
    # Passivity (largest singular value across valid in-band frequency points)
    svs = np.linalg.svd(s_eval, compute_uv=False)
    max_sv = float(np.max(svs)) if svs.size else 0.0
    is_passive = bool(max_sv <= 1.0 + 1e-5)

    # Reciprocity (max |S - S^T| across valid in-band frequency points)
    if n_ports >= 2:
        recip_err = float(np.max(np.abs(s_eval - np.transpose(s_eval, (0, 2, 1)))))
        is_reciprocal = bool(recip_err <= 1e-5)
    else:
        recip_err = 0.0
        is_reciprocal = True

    net_props: dict[str, Any] = {
        "n_ports": n_ports,
        "z0_ohm": float(z0_ohm),
        "is_passive": is_passive,
        "max_singular_value": _finite(max_sv),
        "is_reciprocal": is_reciprocal,
        "max_reciprocity_error": _finite(recip_err),
    }

    # Two-port RF stability metrics (Rollett K, Delta, Edwards-Sinsky mu, MSG/MAG)
    if n_ports == 2:
        s11 = s_eval[:, 0, 0]
        s12 = s_eval[:, 0, 1]
        s21 = s_eval[:, 1, 0]
        s22 = s_eval[:, 1, 1]
        delta = s11 * s22 - s12 * s21
        denom = 2.0 * np.abs(s12 * s21)
        with np.errstate(divide="ignore", invalid="ignore"):
            k_arr = (1.0 - np.abs(s11) ** 2 - np.abs(s22) ** 2 + np.abs(delta) ** 2) / np.where(denom > 1e-18, denom, np.nan)
            mu_denom = np.abs(s22 - delta * np.conj(s11)) + np.abs(s12 * s21)
            mu_arr = (1.0 - np.abs(s11) ** 2) / np.where(mu_denom > 1e-18, mu_denom, np.nan)
            msg_ratio = np.abs(s21) / np.where(np.abs(s12) > 1e-18, np.abs(s12), np.nan)
        valid_k = k_arr[np.isfinite(k_arr)]
        valid_mu = mu_arr[np.isfinite(mu_arr)]
        valid_msg = msg_ratio[np.isfinite(msg_ratio) & (msg_ratio > 0)]
        min_k = float(np.min(valid_k)) if valid_k.size else None
        max_delta = float(np.max(np.abs(delta))) if delta.size else None
        min_mu = float(np.min(valid_mu)) if valid_mu.size else None
        net_props.update({
            "min_rollett_k": _finite(min_k),
            "max_abs_delta": _finite(max_delta),
            "min_mu_stability": _finite(min_mu),
            "unconditionally_stable": bool(min_mu is not None and min_mu > 1.0) if min_mu is not None else None,
            "max_stable_gain_db_max": _finite(10.0 * math.log10(float(np.max(valid_msg)))) if valid_msg.size else None,
        })

    params_out: dict[str, Any] = {}
    for i in range(n_ports):
        for j in range(n_ports):
            key = f"S[{i + 1},{j + 1}]"
            db_curve = s_db[:, i, j]
            deg_curve = s_deg[:, i, j]
            mag_curve = np.abs(s_arr[:, i, j])
            band_valid = band_mask & np.isfinite(db_curve)
            if not np.any(band_valid):
                raise ValueError(f"All samples for '{key}' in requested band are missing/non-finite.")
            band_db = db_curve[band_valid]
            band_freqs = freqs[band_valid]
            band_mags = mag_curve[band_valid]

            min_idx = int(np.argmin(band_db))
            max_idx = int(np.argmax(band_db))
            kp_indices = lttb_downsample_indices(freqs, db_curve, max_points=max_curve_points)

            entry: dict[str, Any] = {
                "min_db": _finite(band_db[min_idx]),
                "freq_at_min_db_hz": _finite(band_freqs[min_idx]),
                "max_db": _finite(band_db[max_idx]),
                "freq_at_max_db_hz": _finite(band_freqs[max_idx]),
                "mean_db": _finite(np.mean(band_db)),
                "ripple_db": _finite(float(band_db[max_idx] - band_db[min_idx])),
                "keypoints_db": [
                    [_finite(freqs[idx]), _finite(db_curve[idx]), _finite(deg_curve[idx])]
                    for idx in kp_indices
                ],
            }

            if i == j:
                vswr_band = vswr[band_valid, i, j]
                # Evaluate matched bands on the FULL in-band grid so missing/NaN points break continuity
                freqs_in_band = freqs[band_mask]
                db_in_band = db_curve[band_mask]
                matched_mask = np.isfinite(freqs_in_band) & np.isfinite(db_in_band) & (db_in_band <= return_loss_spec_db)
                matched = _find_contiguous_bands(freqs_in_band, matched_mask)
                entry.update({
                    "best_match_freq_hz": _finite(band_freqs[min_idx]),
                    "best_return_loss_db": _finite(-float(band_db[min_idx])),
                    "min_vswr": _finite(np.min(vswr_band)),
                    "max_vswr": _finite(np.max(vswr_band)),
                    "return_loss_spec_db": float(return_loss_spec_db),
                    "matched_bands_hz": matched[:4],
                    "matched_bw_hz": _finite(max((b["bw_hz"] for b in matched), default=0.0)),
                })
                if z_mat is not None:
                    global_best_idx = int(np.flatnonzero(band_valid)[min_idx])
                    z_best = complex(z_mat[global_best_idx, i, i])
                    entry["z_at_best_match_ohm"] = {"r": _finite(z_best.real), "x": _finite(z_best.imag)}
            else:
                crossings = _estimate_3db_crossings_np(band_freqs, band_mags)
                entry["crossings_3db_hz"] = [_finite(c) for c in crossings[:6]]
                if len(crossings) >= 2:
                    entry["bw_3db_hz"] = _finite(crossings[-1] - crossings[0])
                if len(band_freqs) >= 2:
                    unwrapped = np.unwrap(np.angle(s_arr[band_valid, i, j]))
                    df = np.diff(band_freqs)
                    dphi = np.diff(unwrapped)
                    valid_gd = np.abs(df) > 0
                    if np.any(valid_gd):
                        gd_s = -dphi[valid_gd] / (2.0 * math.pi * df[valid_gd])
                        entry["group_delay_mean_ns"] = _finite(float(np.mean(gd_s)) * 1e9)

            params_out[key] = entry

    result: dict[str, Any] = {
        "schema": "ads_bridge.rf_compact.v1",
        "kind": "sparameter",
        "engine": engine_versions(),
        "frequency": {
            "start_hz": _finite(freqs[0]) if n_freq else None,
            "stop_hz": _finite(freqs[-1]) if n_freq else None,
            "points": n_freq,
            "in_band_points": int(np.sum(band_mask)),
            "band_hz": [float(band_hz[0]), float(band_hz[1])] if band_hz is not None else None,
        },
        "network_properties": net_props,
        "parameters": params_out,
    }
    if missing_samples > 0:
        result["missing_samples"] = missing_samples
    if exported_touchstone is not None:
        result["touchstone_file"] = exported_touchstone

    raw_bytes_est = max(1, raw_complex_count * 44)
    compact_bytes = len(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    result["compression"] = {
        "raw_samples": raw_complex_count,
        "raw_bytes_est": raw_bytes_est,
        "compact_bytes": compact_bytes,
        "compression_ratio": round(raw_bytes_est / max(1, compact_bytes), 2),
    }
    return result


def compress_touchstone_file(
    path: str | Path,
    *,
    max_curve_points: int = 24,
    band_hz: tuple[float, float] | list[float] | None = None,
    return_loss_spec_db: float = -10.0,
) -> dict[str, Any]:
    """Load any Touchstone (.s1p/.s2p/.sNp) file with scikit-rf and return compact RF JSON."""
    if skrf is None:
        raise RuntimeError("scikit-rf is required for touchstone_compress. Install with `pip install scikit-rf`.")
    p = Path(path).resolve()
    if not p.is_file():
        raise FileNotFoundError(f"Touchstone file not found: {p}")
    ntwk = skrf.Network(str(p))
    z0_val = float(np.real(ntwk.z0[0, 0])) if ntwk.z0.size else 50.0
    out = compress_sparam_network(
        ntwk.f,
        ntwk.s,
        z0_ohm=z0_val,
        max_curve_points=max_curve_points,
        band_hz=band_hz,
        return_loss_spec_db=return_loss_spec_db,
        name=p.stem,
    )
    out["source_file"] = str(p)
    return out


MAX_PHYSICAL_EFFICIENCY_PERCENT = 100.001


def check_pa_efficiency_warnings(
    drain_eff: float | None,
    pae_val: float | None,
    *,
    vds_source: str = "constant",
    vds_v: float | None = None,
) -> list[str]:
    """Shared thermodynamic efficiency sanity check used by both dataset compression and spec scorecards."""
    warnings: list[str] = []
    vds_desc = f"{vds_v:.4g}" if vds_v is not None and math.isfinite(vds_v) else str(vds_v)
    if drain_eff is not None and (not math.isfinite(drain_eff) or drain_eff > MAX_PHYSICAL_EFFICIENCY_PERCENT or drain_eff < 0.0):
        warnings.append(
            f"drain_eff_percent={drain_eff:.2f}% is outside physical bounds [0%, 100%] "
            f"(vds_source={vds_source}, vds_v={vds_desc}); verify vds_signal/vds_v, ids_signal, and load_ohm."
        )
    if pae_val is not None and (not math.isfinite(pae_val) or pae_val > MAX_PHYSICAL_EFFICIENCY_PERCENT):
        warnings.append(
            f"pae_percent={pae_val:.2f}% exceeds 100% physical limit "
            f"(vds_source={vds_source}, vds_v={vds_desc}); for adaptive-bias PAs pass vds_signal instead of a fixed vds_v."
        )
    return warnings


def compress_waveform_block(
    axis_name: str,
    xs_raw: np.ndarray | list[float],
    signals_raw: dict[str, np.ndarray | list[complex]],
    *,
    block_name: str = "",
    max_curve_points: int = 24,
    band_hz: tuple[float, float] | list[float] | None = None,
    load_ohm: float = 50.0,
    return_loss_spec_db: float = -10.0,
    touchstone_path: str | Path | None = None,
    vds_v: float | None = None,
    vds_signal: str | None = None,
    ids_signal: str | None = None,
    vout_signal: str | None = None,
    pin_dbm: float | None = None,
    outer_axes: dict[str, list[Any]] | None = None,
) -> dict[str, Any]:
    """Compress any ADS dataset block (SP, AC, HB, Tran, DC) into compact engineering JSON."""
    if outer_axes:
        axes_desc = ", ".join(
            f"{k} ({len(vals)} values: {vals[:4]}{'...' if len(vals) > 4 else ''})"
            for k, vals in outer_axes.items()
        )
        raise ValueError(
            f"Block '{block_name}' is multi-dimensional with unfiltered outer sweep axis/axes: {axes_desc}. "
            "Pass `filters={axis: value}` to select one slice, or `sweep_axis=<name>` to compress all sweep points."
        )
    xs = np.asarray(xs_raw, dtype=np.float64)
    signals = {k: np.asarray(v, dtype=np.complex128) for k, v in signals_raw.items()}
    if len(xs) == 0:
        raise ValueError(f"Block '{block_name}' has 0 samples.")
    # Guard against flattened 2D frequency/time sweeps even when caller bypassed worker metadata
    if axis_name.lower() in {"freq", "time"} and len(xs) >= 4:
        finite_xs = xs[np.isfinite(xs)]
        if len(finite_xs) >= 4 and np.any(np.diff(finite_xs) < 0.0):
            raise ValueError(
                f"Block '{block_name}' axis '{axis_name}' is non-monotonic (likely an unfiltered 2D ParamSweep). "
                "Specify `filters={outer_var: value}` or `sweep_axis=<outer_var>`."
            )

    # Auto-route full S-parameter matrices to scikit-rf network compressor
    sp_extracted = _extract_sparam_matrix(signals)
    if sp_extracted is not None and axis_name.lower() == "freq":
        _, s_mat = sp_extracted
        out = compress_sparam_network(
            xs,
            s_mat,
            z0_ohm=load_ohm,
            max_curve_points=max_curve_points,
            band_hz=band_hz,
            return_loss_spec_db=return_loss_spec_db,
            touchstone_path=touchstone_path,
            name=block_name or "ads_sparam",
        )
        out["block"] = block_name
        return out

    low_block = block_name.lower()
    low_axis = axis_name.lower()
    # Respect explicit ADS analysis block suffix before falling back to heuristic detection
    is_explicit_ac = ".ac" in low_block or low_block == "ac" or low_block.endswith("_ac")
    is_explicit_hb = ".hb" in low_block or low_block == "hb" or low_block.endswith("_hb")
    is_explicit_tran = ".tran" in low_block or low_block == "tran" or low_axis == "time"
    is_explicit_dc = ".dc" in low_block or low_block == "dc"

    if is_explicit_ac:
        kind = "ac"
    elif is_explicit_hb:
        kind = "harmonic_balance"
    elif is_explicit_tran:
        kind = "transient"
    elif is_explicit_dc:
        kind = "dc"
    elif low_axis == "freq" and len(xs) >= 3 and xs[0] == 0.0 and np.allclose(np.diff(xs), xs[1] - xs[0], rtol=1e-3):
        kind = "harmonic_balance"
    elif low_axis == "freq":
        kind = "ac"
    else:
        kind = "sweep"

    raw_samples = int(len(xs) * len(signals))
    missing_samples = int(sum(np.sum(~np.isfinite(c.real) | ~np.isfinite(c.imag)) for c in signals.values()))
    sig_summaries: dict[str, Any] = {}

    for name, cvals in signals.items():
        valid_mask = np.isfinite(xs) & np.isfinite(cvals.real) & np.isfinite(cvals.imag)
        mags = np.abs(cvals)
        is_current = _is_current_signal(name, ids_signal=ids_signal, vout_signal=vout_signal)

        if kind == "harmonic_balance":
            dc_val = float(cvals[0].real) if (xs[0] == 0.0 and valid_mask[0]) else (0.0 if xs[0] != 0.0 else math.nan)
            f1_idx = 1 if (xs[0] == 0.0 and len(xs) > 1) else 0
            f1_hz = float(xs[f1_idx])
            f1_mag = float(mags[f1_idx]) if valid_mask[f1_idx] else math.nan
            f1_phase = float(math.degrees(math.atan2(float(cvals[f1_idx].imag), float(cvals[f1_idx].real)))) if valid_mask[f1_idx] else math.nan

            # Only compute load power (V^2 / 2R) for voltage signals with known positive load resistance
            can_compute_power = (not is_current) and load_ohm > 0 and math.isfinite(f1_mag)
            p_w = (f1_mag * f1_mag) / (2.0 * load_ohm) if can_compute_power else None
            p_dbm = 10.0 * math.log10(p_w * 1000.0) if (p_w is not None and p_w > 1e-18) else None

            h_start = f1_idx + 1
            harmonics_mag = mags[h_start:] if h_start < len(mags) else np.array([], dtype=np.float64)
            if (
                harmonics_mag.size > 0
                and np.all(np.isfinite(harmonics_mag))
                and math.isfinite(f1_mag)
                and f1_mag > 1e-15
            ):
                thd_pct: float | None = 100.0 * float(np.sqrt(np.sum(harmonics_mag ** 2))) / f1_mag
            else:
                thd_pct = None
            h2_dbc = (20.0 * math.log10(float(mags[f1_idx + 1]) / f1_mag)) if (f1_idx + 1 < len(mags) and valid_mask[f1_idx + 1] and mags[f1_idx + 1] > 0 and math.isfinite(f1_mag) and f1_mag > 0) else None
            h3_dbc = (20.0 * math.log10(float(mags[f1_idx + 2]) / f1_mag)) if (f1_idx + 2 < len(mags) and valid_mask[f1_idx + 2] and mags[f1_idx + 2] > 0 and math.isfinite(f1_mag) and f1_mag > 0) else None

            top_harmonics = []
            for idx in range(f1_idx, min(len(xs), f1_idx + 6)):
                h_entry: dict[str, Any] = {
                    "order": int(idx - f1_idx + 1) if xs[0] == 0.0 else int(idx + 1),
                    "freq_hz": _finite(xs[idx]),
                    "mag": _finite(mags[idx]),
                }
                if (not is_current) and load_ohm > 0 and valid_mask[idx] and mags[idx] > 1e-15:
                    h_entry["dbm"] = _finite(10.0 * math.log10(((float(mags[idx]) ** 2) / (2.0 * load_ohm)) * 1000.0))
                else:
                    h_entry["dbm"] = None
                top_harmonics.append(h_entry)

            sig_summaries[name] = {
                "quantity": "current" if is_current else "voltage",
                "dc": _finite(dc_val),
                "fundamental_hz": _finite(f1_hz),
                "fundamental_mag": _finite(f1_mag),
                "fundamental_phase_deg": _finite(f1_phase),
                "power_w": _finite(p_w),
                "power_dbm": _finite(p_dbm),
                "h2_dbc": _finite(h2_dbc),
                "h3_dbc": _finite(h3_dbc),
                "thd_percent": _finite(thd_pct),
                "harmonics": top_harmonics,
            }

        elif kind in {"transient", "dc", "sweep"}:
            if not np.any(valid_mask):
                raise ValueError(f"All samples for signal '{name}' in block '{block_name}' are missing/non-finite.")
            reals = cvals.real
            valid_reals = reals[valid_mask]
            valid_xs = xs[valid_mask]
            kp_idx = lttb_downsample_indices(valid_xs, valid_reals, max_points=max_curve_points)
            mean_v = float(np.mean(valid_reals))
            rms_v = float(np.sqrt(np.mean(valid_reals ** 2)))
            vmin = float(np.min(valid_reals))
            vmax = float(np.max(valid_reals))
            est_freq = None
            if kind == "transient":
                centered = valid_reals - mean_v
                cross_idx = np.flatnonzero((centered[:-1] <= 0.0) & (centered[1:] > 0.0))
                if len(cross_idx) >= 2:
                    span = float(valid_xs[int(cross_idx[-1])] - valid_xs[int(cross_idx[0])])
                    if span > 0:
                        est_freq = (len(cross_idx) - 1) / span
            sig_summaries[name] = {
                "min": _finite(vmin),
                "max": _finite(vmax),
                "mean": _finite(mean_v),
                "rms": _finite(rms_v),
                "pk_to_pk": _finite(vmax - vmin),
                "estimated_freq_hz": _finite(est_freq),
                "keypoints": [[_finite(valid_xs[i]), _finite(valid_reals[i])] for i in kp_idx],
            }

        else:  # kind == "ac" (always frequency-response analytics, even for purely real resistive networks)
            bmask = _resolve_band_mask(xs, band_hz) & valid_mask
            if not np.any(bmask):
                raise ValueError(f"All samples for AC signal '{name}' in requested band are missing/non-finite.")
            with np.errstate(divide="ignore", invalid="ignore"):
                db_curve = np.where(valid_mask, _db20_arr(np.where(valid_mask, mags, 1.0)), np.nan)
                deg_curve = np.where(valid_mask, np.degrees(np.angle(cvals)), np.nan)
            band_xs = xs[bmask]
            band_mags = mags[bmask]
            band_db = db_curve[bmask]
            band_deg = deg_curve[bmask]

            kp_idx = lttb_downsample_indices(band_xs, band_db, max_points=max_curve_points)
            crossings = _estimate_3db_crossings_np(band_xs, band_mags)
            rolloff = None
            if len(band_xs) >= 2 and band_xs[-1] > band_xs[0] > 0:
                decades = math.log10(float(band_xs[-1]) / float(band_xs[0]))
                if decades >= 0.5:
                    rolloff = float(band_db[-1] - band_db[0]) / decades
            sig_summaries[name] = {
                "mag_max": _finite(np.max(band_mags)),
                "mag_min": _finite(np.min(band_mags)),
                "db20_max": _finite(np.max(band_db)),
                "db20_min": _finite(np.min(band_db)),
                "ripple_db": _finite(float(np.max(band_db) - np.min(band_db))),
                "crossings_3db_hz": [_finite(c) for c in crossings[:6]],
                "rolloff_db_per_dec": _finite(rolloff),
                "keypoints_db": [
                    [_finite(band_xs[i]), _finite(band_db[i]), _finite(band_deg[i])]
                    for i in kp_idx
                ],
            }

    # Optional PA efficiency summary for Harmonic Balance blocks
    # Never fabricate PAE when RF input power (pin_dbm) is unknown: leave pae_percent/gain_db as None.
    pa_metrics: dict[str, Any] | None = None
    if (
        kind == "harmonic_balance"
        and vout_signal
        and vout_signal in sig_summaries
        and sig_summaries[vout_signal].get("quantity") == "voltage"
    ):
        pout_w = sig_summaries[vout_signal].get("power_w")
        resolved_vds: float | None = None
        vds_source = "constant"
        if vds_signal and vds_signal in sig_summaries and sig_summaries[vds_signal].get("quantity") == "voltage":
            vds_dc = sig_summaries[vds_signal].get("dc")
            if vds_dc is not None and math.isfinite(float(vds_dc)) and abs(float(vds_dc)) > 0:
                resolved_vds = abs(float(vds_dc))
                vds_source = f"signal:{vds_signal}"
        elif vds_v is not None:
            resolved_vds = float(vds_v)
        ids_dc = abs(sig_summaries[ids_signal]["dc"]) if (ids_signal and ids_signal in sig_summaries and sig_summaries[ids_signal]["dc"] is not None) else None
        if pout_w is not None and resolved_vds is not None and ids_dc is not None and resolved_vds * ids_dc > 0:
            pdc_w = float(resolved_vds) * float(ids_dc)
            pin_w = (10.0 ** (float(pin_dbm) / 10.0)) * 1e-3 if pin_dbm is not None else None
            drain_eff = 100.0 * pout_w / pdc_w
            pae_val = (100.0 * (pout_w - pin_w) / pdc_w) if pin_w is not None else None
            pa_warnings = check_pa_efficiency_warnings(
                drain_eff,
                pae_val,
                vds_source=vds_source,
                vds_v=resolved_vds,
            )
            pa_metrics = {
                "pout_w": _finite(pout_w),
                "pout_dbm": sig_summaries[vout_signal].get("power_dbm"),
                "vds_v": _finite(resolved_vds),
                "vds_source": vds_source,
                "ids_ma": _finite(ids_dc * 1000.0),
                "pdc_w": _finite(pdc_w),
                "drain_eff_percent": _finite(drain_eff),
                "pae_percent": _finite(pae_val) if pae_val is not None else None,
                "gain_db": _finite(sig_summaries[vout_signal]["power_dbm"] - float(pin_dbm)) if (pin_dbm is not None and sig_summaries[vout_signal].get("power_dbm") is not None) else None,
                "physically_valid": len(pa_warnings) == 0,
            }
            if pa_warnings:
                pa_metrics["warnings"] = pa_warnings

    result: dict[str, Any] = {
        "schema": "ads_bridge.rf_compact.v1",
        "kind": kind,
        "block": block_name,
        "axis": axis_name,
        "axis_range": {"start": _finite(xs[0]), "stop": _finite(xs[-1]), "points": int(len(xs))},
        "engine": engine_versions(),
        "signals": sig_summaries,
    }
    if missing_samples > 0:
        result["missing_samples"] = missing_samples
    if pa_metrics is not None:
        result["pa_metrics"] = pa_metrics

    raw_bytes_est = max(1, raw_samples * 40)
    compact_bytes = len(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    result["compression"] = {
        "raw_samples": raw_samples,
        "raw_bytes_est": raw_bytes_est,
        "compact_bytes": compact_bytes,
        "compression_ratio": round(raw_bytes_est / max(1, compact_bytes), 2),
    }
    return result


def compress_sweep_slices(
    axis_name: str,
    sweep_axis: str,
    slices: list[dict[str, Any]],
    *,
    block_name: str = "",
    max_curve_points: int = 16,
    band_hz: tuple[float, float] | list[float] | None = None,
    load_ohm: float = 50.0,
    return_loss_spec_db: float = -10.0,
    vds_v: float | None = None,
    vds_signal: str | None = None,
    ids_signal: str | None = None,
    vout_signal: str | None = None,
    pin_dbm: float | None = None,
    pin_from_sweep_axis: bool = False,
) -> dict[str, Any]:
    """Compress all slices of a 2D ParamSweep block in one pass (avoids N cold ADS worker launches)."""
    if not slices:
        raise ValueError(f"Sweep block '{block_name}' produced 0 slices along '{sweep_axis}'.")
    rows: list[dict[str, Any]] = []
    for sl in slices:
        sval = sl["sweep_value"]
        eff_pin = float(sval) if (pin_from_sweep_axis and sval is not None) else pin_dbm
        sig_complex = {
            k: decode_complex_pairs(pairs) if (pairs and isinstance(pairs[0], list)) else np.asarray(pairs, dtype=np.complex128)
            for k, pairs in sl["signals"].items()
        }
        comp = compress_waveform_block(
            axis_name,
            sl["axis_values"],
            sig_complex,
            block_name=block_name,
            max_curve_points=max_curve_points,
            band_hz=band_hz,
            load_ohm=load_ohm,
            return_loss_spec_db=return_loss_spec_db,
            vds_v=vds_v,
            vds_signal=vds_signal,
            ids_signal=ids_signal,
            vout_signal=vout_signal,
            pin_dbm=eff_pin,
        )
        entry: dict[str, Any] = {
            sweep_axis: sval,
            "kind": comp["kind"],
            "signals": comp.get("signals") or comp.get("parameters"),
        }
        if "pa_metrics" in comp:
            entry["pa_metrics"] = comp["pa_metrics"]
        rows.append(entry)
    return {
        "schema": "ads_bridge.rf_sweep_compact.v1",
        "block": block_name,
        "axis": axis_name,
        "sweep_axis": sweep_axis,
        "sweep_points": len(rows),
        "sweep_values": [r[sweep_axis] for r in rows],
        "engine": engine_versions(),
        "slices": rows,
    }
