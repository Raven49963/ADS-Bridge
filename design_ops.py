"""ADS design operations for schematics, symbols, layouts, and libraries.

Callable either on the ADS GUI main thread (`de.is_pde_app() == True`) or inside a
standalone ADS Python automation process (`de.is_pde_app() == False`).
"""

from __future__ import annotations

import hashlib
import math
from pathlib import Path
import re
from typing import Any

try:
    from keysight.ads import de
    from keysight.ads.de import db_uu
except ImportError:  # Allow importing pure geometry/validation helpers outside ADS Python
    de = None  # type: ignore[assignment]
    db_uu = None  # type: ignore[assignment]

from bridge import BridgeError, ParameterValue
import netlist_parser


_TERM_DIRECTIONS = (
    {
        "input": de.db.TermType.INPUT,
        "output": de.db.TermType.OUTPUT,
        "inputoutput": de.db.TermType.INPUT_OUTPUT,
        "inout": de.db.TermType.INPUT_OUTPUT,
    }
    if de is not None
    else {}
)


def lcv_name(lib: str, cell: str, view: str) -> str:
    return f"{lib}:{cell}:{view}"


def split_lcv(value: str, valid_views: set[str]) -> tuple[str, str, str]:
    if not isinstance(value, str) or value.count(":") != 2:
        raise BridgeError("invalid_design", "Use a library:cell:view design name.")
    lib, cell, view = value.split(":")
    if not lib or not cell or view not in valid_views:
        raise BridgeError("invalid_design", f"Expected a nonempty library/cell and one of {sorted(valid_views)}.")
    return lib, cell, view


def normalize_symbol_lcv(value: str) -> tuple[str, str, str]:
    if not isinstance(value, str):
        raise BridgeError("invalid_design", "Component master must be 'library:cell' or 'library:cell:symbol'.")
    if value.count(":") == 1:
        value = f"{value}:symbol"
    return split_lcv(value, {"symbol"})


def new_name(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", value):
        raise BridgeError("invalid_name", "New ADS library, cell, and instance names must match [A-Za-z][A-Za-z0-9_]{0,63}.")
    return value


def valid_net_label(label: str) -> str:
    if not isinstance(label, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_!.]{0,63}", label):
        raise BridgeError("invalid_manifest", f"Invalid wire or pin net label: {label!r}.")
    return label


def number(value: Any) -> float:
    if type(value) not in (int, float) or abs(value) > 1e12 or not math.isfinite(value):
        raise BridgeError("invalid_manifest", "Coordinates and angles must be finite numbers within ±1e12.")
    return float(value)


def point(value: Any) -> tuple[float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise BridgeError("invalid_manifest", "Each point must be [x, y] in ADS user units.")
    return number(value[0]), number(value[1])


def points(value: Any, minimum: int = 2) -> list[tuple[float, float]]:
    if not isinstance(value, list) or not minimum <= len(value) <= 256:
        raise BridgeError("invalid_manifest", f"Expected between {minimum} and 256 points.")
    return [point(item) for item in value]


def records(items: Any, limit: int, name: str) -> list[dict[str, Any]]:
    if not isinstance(items, list) or len(items) > limit or any(not isinstance(item, dict) for item in items):
        raise BridgeError("invalid_manifest", f"{name} must be an array with up to {limit} objects.")
    return items


def _parameter_string(value: Any) -> bool:
    return isinstance(value, str) and len(value) <= 256 and not any(c in value for c in "\n\r\x00")


def parameter_values(values: Any, *, allow_arrays: bool = True) -> dict[str, ParameterValue]:
    if not isinstance(values, dict) or len(values) > 100:
        raise BridgeError("invalid_manifest", "Parameters must be an object with at most 100 entries.")
    for name, value in values.items():
        if not isinstance(name, str) or not name or len(name) > 80 or not _parameter_string(name):
            raise BridgeError("invalid_manifest", "Parameter names must be bounded single-line strings.")
        if _parameter_string(value):
            continue
        if allow_arrays and isinstance(value, list) and len(value) <= 128 and all(
            _parameter_string(entry)
            or (isinstance(entry, list) and len(entry) <= 128 and all(map(_parameter_string, entry)))
            for entry in value
        ):
            continue
        kind = "a single-line string or a bounded array of strings or compound rows" if allow_arrays else "a single-line string"
        raise BridgeError("invalid_manifest", f"Parameter {name!r} must be {kind}.")
    return values


def get_view(workspace: Any, library: str, cell: str, view: str) -> Any:
    if library not in workspace.library_names:
        return None
    obj = workspace.open_library(library).get_cell_if_exists(cell)
    return obj.get_view_if_exists(view) if obj is not None else None


def box_values(box: Any) -> dict[str, float]:
    return {"left": float(box.x1), "bottom": float(box.y1), "right": float(box.x2), "top": float(box.y2)}


def master_lcv_str(instance: Any) -> str:
    name = instance.master_lcv_name
    return f"{name.library_name}:{name.cell_name}:{name.view_name}"


def safe_inst_term_identity(inst_term: Any) -> tuple[str, str, int | None]:
    """Return (term_id, term_name, term_number) without triggering ADS C++ numbered-term RuntimeError."""
    is_numbered = bool(getattr(inst_term, "is_numbered", False))
    if is_numbered:
        num = int(inst_term.term_number)
        underlying_name = getattr(getattr(inst_term, "term", None), "name", None) or f"P{num}"
        return str(num), str(underlying_name), num
    name = str(inst_term.term_name)
    num_raw = getattr(inst_term, "term_number", None)
    num = int(num_raw) if isinstance(num_raw, int) and num_raw > 0 else None
    return name, name, num


def inst_term_snap_point(inst_term: Any) -> tuple[float, float] | None:
    pins = list(getattr(inst_term, "inst_pins", ()))
    if not pins:
        return None
    snap = pins[0].snap_point
    return (float(snap.x), float(snap.y))


def resolve_instance_terminal(instance: Any, selector: str | int) -> tuple[Any, tuple[float, float]]:
    """Find an InstTerm on `instance` by pin number ('1'), pin name ('P1'), or 1-based index."""
    token = str(selector).strip()
    for idx, it in enumerate(instance.inst_terms, start=1):
        tid, tname, tnum = safe_inst_term_identity(it)
        if token in {tid, tname} or (tnum is not None and token == str(tnum)) or token == f"#{idx}":
            pt = inst_term_snap_point(it)
            if pt is None:
                raise BridgeError("pin_not_found", f"Terminal {instance.name}.{selector} has no physical pin snap point.")
            return it, pt
    available = [
        f"{tid}({tname})" if tid != tname else tid
        for tid, tname, _ in (safe_inst_term_identity(it) for it in instance.inst_terms)
    ]
    raise BridgeError(
        "pin_not_found",
        f"Unknown terminal '{selector}' on instance '{instance.name}'. Available: {available}",
    )


def _point_on_segment(pt: tuple[float, float], a: tuple[float, float], b: tuple[float, float], tol: float = 1e-6) -> bool:
    px, py = pt
    ax, ay = a
    bx, by = b
    if abs(ax - bx) <= tol:
        return abs(px - ax) <= tol and min(ay, by) - tol <= py <= max(ay, by) + tol
    if abs(ay - by) <= tol:
        return abs(py - ay) <= tol and min(ax, bx) - tol <= px <= max(ax, bx) + tol
    return False


def _segments_overlap_or_touch(
    a1: tuple[float, float],
    a2: tuple[float, float],
    b1: tuple[float, float],
    b2: tuple[float, float],
    p1: tuple[float, float],
    p2: tuple[float, float],
    tol: float = 1e-6,
) -> bool:
    """Detect collinear overlap or perpendicular interior crossing between two orthogonal wire segments."""
    def _is_endpoint(pt: tuple[float, float]) -> bool:
        return (abs(pt[0] - p1[0]) <= tol and abs(pt[1] - p1[1]) <= tol) or (
            abs(pt[0] - p2[0]) <= tol and abs(pt[1] - p2[1]) <= tol
        )

    a_vert = abs(a1[0] - a2[0]) <= tol
    b_vert = abs(b1[0] - b2[0]) <= tol
    if a_vert and b_vert:
        if abs(a1[0] - b1[0]) > tol:
            return False
        lo = max(min(a1[1], a2[1]), min(b1[1], b2[1]))
        hi = min(max(a1[1], a2[1]), max(b1[1], b2[1]))
        if hi - lo > tol:
            return True  # collinear overlap merges nets in OpenAccess!
        if abs(hi - lo) <= tol:
            return not _is_endpoint((a1[0], lo))
        return False
    if (not a_vert) and (not b_vert):
        if abs(a1[1] - b1[1]) > tol:
            return False
        lo = max(min(a1[0], a2[0]), min(b1[0], b2[0]))
        hi = min(max(a1[0], a2[0]), max(b1[0], b2[0]))
        if hi - lo > tol:
            return True
        if abs(hi - lo) <= tol:
            return not _is_endpoint((lo, a1[1]))
        return False
    # One vertical, one horizontal: a T-junction (endpoint of one touches interior of other) merges nets in OA!
    vx, vy1, vy2 = (a1[0], min(a1[1], a2[1]), max(a1[1], a2[1])) if a_vert else (b1[0], min(b1[1], b2[1]), max(b1[1], b2[1]))
    hy, hx1, hx2 = (b1[1], min(b1[0], b2[0]), max(b1[0], b2[0])) if a_vert else (a1[1], min(a1[0], a2[0]), max(a1[0], a2[0]))
    if hx1 - tol <= vx <= hx2 + tol and vy1 - tol <= hy <= vy2 + tol:
        ix = (vx, hy)
        if _is_endpoint(ix):
            return False
        # Strict crossing (X) does not merge nets in OA, whereas any vertex landing on a wire (T) DOES merge nets!
        for pt in (a1, a2, b1, b2):
            if abs(pt[0] - ix[0]) <= tol and abs(pt[1] - ix[1]) <= tol:
                return True
    return False


def _path_hits_obstacle(
    verts: list[tuple[float, float]],
    obstacles: set[tuple[float, float]],
    p1: tuple[float, float],
    p2: tuple[float, float],
    tol: float = 1e-6,
    blocked_segments: list[tuple[tuple[float, float], tuple[float, float]]] | None = None,
) -> bool:
    if obstacles:
        for obs in obstacles:
            if (abs(obs[0] - p1[0]) <= tol and abs(obs[1] - p1[1]) <= tol) or (
                abs(obs[0] - p2[0]) <= tol and abs(obs[1] - p2[1]) <= tol
            ):
                continue
            for a, b in zip(verts, verts[1:]):
                if _point_on_segment(obs, a, b, tol=tol):
                    return True
    if blocked_segments:
        for a, b in zip(verts, verts[1:]):
            for s1, s2 in blocked_segments:
                if _segments_overlap_or_touch(a, b, s1, s2, p1, p2, tol=tol):
                    return True
    return False


def _collect_pin_obstacles(design: Any) -> set[tuple[float, float]]:
    pts: set[tuple[float, float]] = set()
    for inst in getattr(design, "instances", ()):
        for it in getattr(inst, "inst_terms", ()):
            pt = inst_term_snap_point(it)
            if pt is not None:
                pts.add((round(pt[0], 6), round(pt[1], 6)))
    for term in getattr(design, "terms", ()):
        for pin in getattr(term, "pins", ()):
            snap = pin.snap_point
            pts.add((round(float(snap.x), 6), round(float(snap.y), 6)))
    return pts


def route_orthogonal(
    p1: tuple[float, float],
    p2: tuple[float, float],
    obstacles: set[tuple[float, float]] | None = None,
    blocked_segments: list[tuple[tuple[float, float], tuple[float, float]]] | None = None,
) -> list[tuple[float, float]]:
    """Generate obstacle- and wire-segment-aware orthogonal Manhattan vertices between two snap points."""
    x1, y1 = p1
    x2, y2 = p2
    if abs(x1 - x2) < 1e-9 and abs(y1 - y2) < 1e-9:
        return [p1]

    obs = obstacles or set()
    segs = blocked_segments or []
    # 1. Direct straight line when axis-aligned
    if abs(x1 - x2) < 1e-9 or abs(y1 - y2) < 1e-9:
        direct = [p1, p2]
        if not _path_hits_obstacle(direct, obs, p1, p2, blocked_segments=segs):
            return direct
        # Blocked straight line (e.g. passing through a middle pin or collinear wire): try perpendicular U-detours
        for offset in (0.5, -0.5, 0.75, -0.75, 1.0, -1.0, 1.5, -1.5, 2.0, -2.0):
            if abs(x1 - x2) < 1e-9:
                cand = [p1, (x1 + offset, y1), (x2 + offset, y2), p2]
            else:
                cand = [p1, (x1, y1 + offset), (x2, y2 + offset), p2]
            if not _path_hits_obstacle(cand, obs, p1, p2, blocked_segments=segs):
                return cand
        return direct

    # 2. Try both single-bend L paths
    cand_hv = [p1, (x2, y1), p2]
    if not _path_hits_obstacle(cand_hv, obs, p1, p2, blocked_segments=segs):
        return cand_hv
    cand_vh = [p1, (x1, y2), p2]
    if not _path_hits_obstacle(cand_vh, obs, p1, p2, blocked_segments=segs):
        return cand_vh

    # 3. Both L-bends hit an obstacle pin or wire: try 3-segment Z detours around obstacles
    mid_x = 0.5 * (x1 + x2)
    mid_y = 0.5 * (y1 + y2)
    detours = [
        [p1, (mid_x, y1), (mid_x, y2), p2],
        [p1, (x1, mid_y), (x2, mid_y), p2],
    ]
    for off in (0.5, -0.5, 0.75, -0.75, 1.0, -1.0, 1.5, -1.5):
        detours.append([p1, (x1 + off, y1), (x1 + off, y2), p2])
        detours.append([p1, (x1, y1 + off), (x2, y1 + off), p2])
        detours.append([p1, (x2 + off, y1), (x2 + off, y2), p2])
        detours.append([p1, (x1, y2 + off), (x2, y2 + off), p2])
    for cand in detours:
        if not _path_hits_obstacle(cand, obs, p1, p2, blocked_segments=segs):
            return cand
    return cand_hv


def _parameter_shape(value: ParameterValue) -> Any:
    if isinstance(value, list):
        return [_parameter_shape(entry) for entry in value]
    return "string"


def _model_default_value(item: Any) -> Any:
    if item is None:
        return None
    if hasattr(item, "repeats"):
        return [_model_default_value(row) for row in item.repeats]
    if hasattr(item, "sub_params"):
        return [_model_default_value(field) for field in item.sub_params]
    value = getattr(item, "value", None)
    if value is not None:
        return value
    raw = str(item)
    # Clean ADS internal repr like '<ParamItemConst "NMOS"=form("y_n1")>'
    m = re.search(r'form\("([^"]+)"\)', raw)
    if m:
        return m.group(1)
    return raw


def apply_instance_parameters(instance: Any, values: dict[str, ParameterValue]) -> list[dict[str, Any]]:
    """Apply complete parameter values and verify structured readback inside the caller's transaction."""
    changes = []
    for parameter, value in values.items():
        if parameter not in instance.parameters:
            available = [p.name for p in instance.parameters]
            raise BridgeError(
                "invalid_parameter",
                f"Instance '{instance.name}' ({master_lcv_str(instance)}) has no parameter '{parameter}'. "
                f"Available: {available}",
            )
        item = instance.parameters[parameter]
        old = item.value
        repeated = hasattr(item, "repeats")
        legacy_single_repeat = repeated and isinstance(value, str) and _parameter_shape(old) == ["string"]
        requested = [value] if legacy_single_repeat else value
        expected_shape = _parameter_shape(old)
        if _parameter_shape(requested) != expected_shape:
            raise BridgeError(
                "invalid_parameter_shape",
                f"{instance.name}.{parameter} expects {expected_shape!r}. Supply every ADS repeat and compound field "
                "in order, using design.read to inspect the current value.",
            )
        changes.append((parameter, old, requested, repeated, legacy_single_repeat))

    for parameter, _, requested, repeated, _ in changes:
        item = instance.parameters[parameter]
        try:
            if repeated:
                for repeat, row in zip(item.repeats, requested, strict=True):
                    repeat.value = row
            else:
                item.value = requested
        except (RuntimeError, TypeError, ValueError) as exc:
            raise BridgeError("invalid_parameter", f"ADS rejected {instance.name}.{parameter}: {exc}") from exc

    if changes and hasattr(instance, "invoke_item_parameter_changed_callback"):
        try:
            instance.invoke_item_parameter_changed_callback(list(values))
        except Exception:
            # Built-in primitives without AEL/Python item callbacks can safely ignore callback errors.
            pass

    applied = []
    for parameter, old, requested, _, legacy_single_repeat in changes:
        actual = instance.parameters[parameter].value
        if isinstance(requested, list) and actual != requested:
            raise BridgeError(
                "parameter_readback_mismatch",
                f"ADS readback for {instance.name}.{parameter} was {actual!r}, expected {requested!r}.",
            )
        if legacy_single_repeat:
            old, actual = old[0], actual[0]
        applied.append({"instance": instance.name, "parameter": parameter, "old_value": old, "value": actual})
    return applied


_PIN_POLARITY_HINTS: dict[str, tuple[dict[int, str], str]] = {
    "V_DC": ({1: "positive (+)", 2: "negative (- / GND)"}, "Pin 1 is +Vdc, Pin 2 is -/GND."),
    "V_AC": ({1: "positive (+)", 2: "negative (- / GND)"}, "Pin 1 is +Vac, Pin 2 is -/GND."),
    "V_Sine": ({1: "positive (+)", 2: "negative (- / GND)"}, "Pin 1 is +signal, Pin 2 is -/GND."),
    "P_1Tone": ({1: "RF output (+)", 2: "reference (- / GND)"}, "Pin 1 is +RF port, Pin 2 is GND."),
    "Port": ({1: "signal (+)", 2: "reference (- / GND)"}, "Pin 1 is port signal, Pin 2 is GND."),
    "Term": ({1: "signal (+)", 2: "reference (- / GND)"}, "Pin 1 is port signal, Pin 2 is GND."),
    "TermG": ({1: "signal (+)"}, "Single-ended grounded RF port."),
    "Diode": ({1: "anode (+)", 2: "cathode (-)"}, "Pin 1 is anode, Pin 2 is cathode (reverse-bias: Pin 2 -> high V, Pin 1 -> GND)."),
    "I_Probe": ({1: "current_in (+)", 2: "current_out (-)"}, "Positive current flows from Pin 1 -> Pin 2."),
    "MOSFET_NMOS": ({1: "drain", 2: "gate", 3: "source", 4: "bulk"}, "1=drain, 2=gate, 3=source, 4=bulk."),
    "MOSFET_PMOS": ({1: "drain", 2: "gate", 3: "source", 4: "bulk"}, "1=drain, 2=gate, 3=source, 4=bulk."),
    "SDD1P": ({1: "port1_pos (_v1+)", 2: "port1_neg (_v1-)"}, "Port 1: (1+, 2-)."),
    "SDD2P": ({1: "port1_pos (_v1+)", 2: "port1_neg (_v1-)", 3: "port2_pos (_v2+)", 4: "port2_neg (_v2-)"}, "Odd pins (1,3) are +ports, even pins (2,4) are -references; Port 1 is on left, Port 2 is on right."),
    "SDD3P": ({1: "port1_pos (_v1+)", 2: "port1_neg (_v1-)", 3: "port2_pos (_v2+)", 4: "port2_neg (_v2-)", 5: "port3_pos (_v3+)", 6: "port3_neg (_v3-)"}, "Odd pins (1,3,5) are +ports, even pins (2,4,6) are -references; Port 1=left, Port 2=right, Port 3=bottom-left."),
}


def describe_component(workspace: Any, master: str, *, compact: bool = False) -> dict[str, Any]:
    """Inspect an ADS component's symbol pins, snap offsets, polarity hints, and parameter definitions."""
    lib_name, cell_name, view_name = normalize_symbol_lcv(master)
    if get_view(workspace, lib_name, cell_name, view_name) is None:
        raise BridgeError("master_not_found", f"Component symbol {lib_name}:{cell_name}:{view_name} was not found.")

    role_map, polarity_hint = _PIN_POLARITY_HINTS.get(cell_name, ({}, ""))
    full_lcv = lcv_name(lib_name, cell_name, view_name)
    sym = db_uu.open_design(full_lcv, db_uu.DesignMode.READ_ONLY)
    try:
        terminals: list[dict[str, Any]] = []
        for term in sym.terms:
            num = int(term.number) if getattr(term, "number", None) is not None else None
            pins_info = []
            for pin in getattr(term, "pins", ()):
                snap = pin.snap_point
                pins_info.append({
                    "at": [float(snap.x), float(snap.y)],
                    "angle": float(getattr(pin, "angle", 0.0)),
                })
            t_entry: dict[str, Any] = {
                "number": num,
                "name": str(term.name),
                "direction": str(term.term_type).split(".")[-1],
                "pins": pins_info,
            }
            if num in role_map:
                t_entry["role"] = role_map[num]
            terminals.append(t_entry)
        terminals.sort(key=lambda item: (item["number"] is None, item["number"] or 0, item["name"]))
        sym_bbox = box_values(sym.bbox)
    finally:
        # Release transient read-only symbol handles immediately in headless sessions,
        # or in GUI sessions when the symbol has no open editor window and is unmodified.
        should_close = not de.is_pde_app()
        if not should_close:
            try:
                from keysight.ads.de import app
                has_win = bool(app.find_windows_by_design(sym)) if hasattr(app, "find_windows_by_design") else True
                should_close = not has_win and not bool(getattr(sym, "is_modified", False))
            except Exception:
                should_close = False
        if should_close:
            try:
                if getattr(sym, "is_valid", True):
                    sym.close_design()
            except Exception:
                pass

    cell = workspace.open_library(lib_name).get_cell_if_exists(cell_name)
    mdef = getattr(cell, "model_def", None) if cell is not None else None
    params_info: list[dict[str, Any]] = []
    flags: dict[str, bool] = {}
    label = cell_name
    prefix = cell_name[:1].upper()

    if mdef is not None:
        label = str(getattr(mdef, "label", "") or cell_name)
        prefix = str(getattr(mdef, "inst_name_prefix", "") or prefix)
        for flag_name in (
            "is_ground", "is_variable", "is_transmission_line", "is_sub_design",
            "is_bom_item", "is_smart_component", "is_unique",
        ):
            if hasattr(mdef, flag_name):
                flags[flag_name] = bool(getattr(mdef, flag_name))
        for mp in getattr(mdef, "parameters", ()):
            default_val = _model_default_value(getattr(mp, "default_value", None))
            params_info.append({
                "name": str(mp.name),
                "label": str(getattr(mp, "label", "") or mp.name),
                "default": default_val,
                "input_shape": _parameter_shape(default_val) if default_val is not None else None,
                "unit_type": str(getattr(mp, "unit_type", "")).split(".")[-1],
                "editable": bool(getattr(mp, "is_editable", True)),
                "optimizable": bool(getattr(mp, "is_optimizable", False)),
                "netlistable": bool(getattr(mp, "is_netlistable", True)),
                "displayed_by_default": bool(getattr(mp, "is_displayed_by_default", False)),
            })

    if compact:
        params_info = [
            {
                "name": p["name"],
                "default": p["default"],
                "input_shape": p["input_shape"],
            }
            for p in params_info
            if p["displayed_by_default"] or (p["default"] not in (None, "", [""]))
        ]

    out: dict[str, Any] = {
        "master": full_lcv,
        "library": lib_name,
        "cell": cell_name,
        "label": label,
        "prefix": prefix,
        "flags": flags,
        "symbol_bbox": sym_bbox,
        "terminals": terminals,
        "parameters": params_info,
    }
    if polarity_hint:
        out["polarity_hint"] = polarity_hint
    return out


def read_schematic_details(target: Any, *, include_netlist_ast: bool = True) -> dict[str, Any]:
    """Read a schematic design with rich pin coordinates, port metadata, and netlist AST."""
    try:
        netlist_text = target.generate_netlist()
        revision = hashlib.sha256(netlist_text.encode("utf-8")).hexdigest()
        diagnostic = None
    except RuntimeError as exc:
        netlist_text = None
        revision = None
        diagnostic = str(exc)

    instances_out: list[dict[str, Any]] = []
    for instance in target.instances:
        is_var = bool(getattr(instance, "is_var_instance", False))
        var_dict = dict(instance.vars) if is_var else {}
        terms_out = []
        conn_nets = []
        for it in instance.inst_terms:
            tid, tname, tnum = safe_inst_term_identity(it)
            net_name = it.net.name if it.net is not None else None
            conn_nets.append(net_name)
            pt = inst_term_snap_point(it)
            terms_out.append({
                "id": tid,
                "name": tname,
                "number": tnum,
                "net": net_name,
                "at": list(pt) if pt is not None else None,
            })
        origin = instance.origin
        instances_out.append({
            "name": instance.name,
            "master": master_lcv_str(instance),
            "origin": [float(origin.x), float(origin.y)],
            "orient": str(instance.orient).split(".")[-1],
            "bbox": box_values(instance.bbox),
            "is_var_instance": is_var,
            "variables": var_dict,
            "parameters": {item.name: item.value for item in instance.parameters},
            "connection_nets": conn_nets,
            "terminals": terms_out,
        })

    ports_out: list[dict[str, Any]] = []
    for term in target.terms:
        pin_pts = [[float(p.snap_point.x), float(p.snap_point.y)] for p in getattr(term, "pins", ())]
        ports_out.append({
            "name": str(term.name),
            "number": int(term.number) if getattr(term, "number", None) is not None else None,
            "direction": str(term.term_type).split(".")[-1],
            "net": term.net.name if term.net is not None else None,
            "pins": pin_pts,
        })

    payload: dict[str, Any] = {
        "design": str(target.design_name),
        "revision": revision,
        "netlist_diagnostic": diagnostic,
        "instances": instances_out,
        "ports": ports_out,
        "nets": [net.name for net in target.nets],
    }
    if include_netlist_ast and netlist_text:
        try:
            parsed = netlist_parser.inspect_netlist(netlist_text)
            payload["ai_summary"] = parsed["ai_summary"]
            payload["circuit_insights"] = parsed["circuit_insights"]
            payload["analyses"] = parsed["analyses"]
        except netlist_parser.NetlistError:
            pass
    return payload


def _parse_endpoint_spec(spec: dict[str, Any], side: str) -> tuple[str, str] | None:
    """Parse wire endpoint either from `from`/`to` ('R1.2' or 'PIN:IN') or `from_inst`+`from_term`."""
    shorthand = spec.get(side)
    if shorthand is not None:
        if not isinstance(shorthand, str) or not shorthand.strip():
            raise BridgeError("invalid_manifest", f"Wire '{side}' endpoint must be 'Inst.Term' or 'PIN:Name'.")
        if shorthand.upper().startswith("PIN:"):
            pin_name = shorthand.split(":", 1)[1].strip()
            if not pin_name:
                raise BridgeError("invalid_manifest", f"Empty pin name in wire endpoint {shorthand!r}.")
            return ("PIN", pin_name)
        if "." not in shorthand:
            raise BridgeError("invalid_manifest", f"Wire '{side}' endpoint {shorthand!r} must have form 'Inst.Term'.")
        inst_name, term_sel = shorthand.split(".", 1)
        return (inst_name.strip(), term_sel.strip())

    inst_key = f"{side}_inst"
    term_key = f"{side}_term"
    if inst_key in spec or term_key in spec:
        inst_name = spec.get(inst_key)
        term_sel = spec.get(term_key)
        if not isinstance(inst_name, str) or not inst_name or term_sel is None or str(term_sel) == "":
            raise BridgeError("invalid_manifest", f"Provide both '{inst_key}' and '{term_key}'.")
        return (inst_name.strip(), str(term_sel).strip())
    return None


def _resolve_endpoint_point(
    design: Any,
    pin_locations: dict[str, tuple[float, float]],
    endpoint: tuple[str, str],
) -> tuple[float, float]:
    kind_or_inst, selector = endpoint
    # Prefer actual instance name first so instances named e.g. 'PIN' do not collide with schematic pin syntax!
    if kind_or_inst in design.instances:
        _, pt = resolve_instance_terminal(design.instances[kind_or_inst], selector)
        return pt
    if kind_or_inst == "PIN":
        if selector in pin_locations:
            return pin_locations[selector]
        for term in design.terms:
            if term.name == selector and getattr(term, "pins", ()):
                snap = term.pins[0].snap_point
                return (float(snap.x), float(snap.y))
        raise BridgeError("pin_not_found", f"Schematic pin '{selector}' was not found.")
    raise BridgeError("instance_not_found", f"Instance '{kind_or_inst}' does not exist in schematic.")


def _add_schematic_pin(
    design: Any,
    pin_spec: dict[str, Any],
    pin_locations: dict[str, tuple[float, float]],
) -> dict[str, Any]:
    name = valid_net_label(pin_spec["name"])
    direction_raw = str(pin_spec.get("direction", "inputOutput")).strip().lower()
    if direction_raw not in _TERM_DIRECTIONS:
        raise BridgeError("invalid_manifest", f"Pin direction must be input, output, or inputOutput; got {direction_raw!r}.")
    term_type = _TERM_DIRECTIONS[direction_raw]

    connect_target: tuple[str, str] | None = None
    if "connect" in pin_spec and pin_spec["connect"] is not None:
        connect_target = _parse_endpoint_spec({"from": pin_spec["connect"]}, "from")
    elif "at_inst" in pin_spec or "at_term" in pin_spec:
        connect_target = _parse_endpoint_spec(
            {"from_inst": pin_spec.get("at_inst"), "from_term": pin_spec.get("at_term")},
            "from",
        )

    target_pt = _resolve_endpoint_point(design, pin_locations, connect_target) if connect_target else None
    if "at" in pin_spec and pin_spec["at"] is not None:
        loc = point(pin_spec["at"])
    elif target_pt is not None:
        offset_x = -1.0 if direction_raw == "input" else 1.0
        loc = (target_pt[0] + offset_x, target_pt[1])
    else:
        raise BridgeError("invalid_manifest", f"Pin '{name}' requires either 'at': [x, y] or 'connect': 'Inst.Term'.")

    net = design.find_or_add_net(name)
    existing_terms = {t.name for t in design.terms}
    if name in existing_terms:
        raise BridgeError("invalid_manifest", f"Schematic pin '{name}' already exists.")
    if "number" in pin_spec and pin_spec["number"] is not None:
        num = int(pin_spec["number"])
        term = design.add_numbered_term(net, name, num, term_type)
    else:
        term = design.add_term(net, name, term_type)
    dot = design.add_dot_for_pin(loc)
    angle = number(pin_spec.get("angle", 0.0))
    design.add_pin(term, dot, angle=angle)
    pin_locations[name] = loc

    if target_pt is not None:
        verts = route_orthogonal(loc, target_pt, obstacles=_collect_pin_obstacles(design))
        if len(verts) >= 2:
            wire = design.add_wire(verts)
            wire.add_wire_label(name)
    return {"name": name, "direction": direction_raw, "at": list(loc)}


def _add_schematic_wire(
    design: Any,
    wire_spec: dict[str, Any],
    pin_locations: dict[str, tuple[float, float]],
    routed_segments: list[tuple[tuple[float, float], tuple[float, float]]] | None = None,
) -> dict[str, Any]:
    label = wire_spec.get("label") if wire_spec.get("label") is not None else wire_spec.get("net")
    if label is not None:
        valid_net_label(label)

    from_ep = _parse_endpoint_spec(wire_spec, "from")
    to_ep = _parse_endpoint_spec(wire_spec, "to")
    if from_ep is not None or to_ep is not None:
        if from_ep is None or to_ep is None:
            raise BridgeError("invalid_manifest", "Endpoint wiring requires both 'from' and 'to' endpoints.")
        p1 = _resolve_endpoint_point(design, pin_locations, from_ep)
        p2 = _resolve_endpoint_point(design, pin_locations, to_ep)
        vertices = route_orthogonal(
            p1,
            p2,
            obstacles=_collect_pin_obstacles(design),
            blocked_segments=routed_segments,
        )
    elif "points" in wire_spec:
        vertices = points(wire_spec["points"])
    else:
        raise BridgeError("invalid_manifest", "Each wire requires either 'points' or 'from' and 'to' endpoints.")

    if len(vertices) >= 2:
        wire = design.add_wire(vertices)
        if label is not None:
            wire.add_wire_label(label)
        if routed_segments is not None:
            for a, b in zip(vertices, vertices[1:]):
                routed_segments.append((a, b))
    elif len(vertices) == 1 and label is not None:
        # Coincident snap points are already electrically shorted by OpenAccess; attach net label cleanly.
        design.add_net_connection_label(vertices[0], "netName", label)
    return {"points": [list(v) for v in vertices], "label": label}


def _set_var_block(design: Any, name: str, values: dict[str, str], at: tuple[float, float] = (-4.0, -3.0)) -> dict[str, Any]:
    params = parameter_values(values, allow_arrays=False)
    if not params:
        raise BridgeError("invalid_manifest", "Variables object must contain at least one entry.")
    target_inst = None
    created_new = False
    if name in design.instances:
        cand = design.instances[name]
        if not bool(getattr(cand, "is_var_instance", False)):
            raise BridgeError("invalid_manifest", f"Instance '{name}' exists and is not a VAR block.")
        target_inst = cand
    else:
        new_name(name)
        target_inst = design.add_var_instance(at, name=name)
        created_new = True
    for k, v in params.items():
        new_name(k)
        target_inst.vars[k] = v
    # Remove ADS's default placeholder variable X=1.0 on newly created VAR blocks unless explicitly requested
    if created_new and "X" not in params and "X" in target_inst.vars:
        try:
            del target_inst.vars["X"]
        except Exception:
            pass
    return {"instance": target_inst.name, "variables": dict(target_inst.vars)}


def schematic_plan(
    workspace: Any,
    instances: list[dict[str, Any]],
    wires: list[dict[str, Any]],
    pins: list[dict[str, Any]] | None = None,
    variables: dict[str, str] | None = None,
) -> tuple[list[Any], list[dict[str, Any]], list[dict[str, Any]], dict[str, str]]:
    names: set[str] = set()
    placements: list[Any] = []
    for item in records(instances, 300, "instances"):
        name = new_name(item["name"])
        master = normalize_symbol_lcv(item["master"])
        if name in names:
            raise BridgeError("invalid_manifest", f"Repeated instance {name}.")
        if get_view(workspace, *master) is None:
            raise BridgeError("master_not_found", f"Cannot find {lcv_name(*master)}.")
        names.add(name)
        placements.append((
            name,
            master,
            point(item["at"]),
            number(item.get("angle", 0)),
            parameter_values(item.get("parameters", {})),
        ))

    pin_specs = records(pins or [], 128, "pins")
    seen_pins: set[str] = set()
    for p in pin_specs:
        pname = valid_net_label(p["name"])
        if pname in seen_pins:
            raise BridgeError("invalid_manifest", f"Repeated schematic pin {pname}.")
        seen_pins.add(pname)
        if "direction" in p and str(p["direction"]).strip().lower() not in _TERM_DIRECTIONS:
            raise BridgeError("invalid_manifest", f"Unsupported pin direction {p['direction']!r}.")

    wire_specs = records(wires, 600, "wires")
    for w in wire_specs:
        if "points" in w and "from" not in w and "from_inst" not in w:
            points(w["points"])
        else:
            f_ep = _parse_endpoint_spec(w, "from")
            t_ep = _parse_endpoint_spec(w, "to")
            if f_ep is None or t_ep is None:
                raise BridgeError("invalid_manifest", "Each wire must specify 'points' or both 'from' and 'to'.")
        w_label = w.get("label") if w.get("label") is not None else w.get("net")
        if w_label is not None:
            valid_net_label(w_label)

    var_values = parameter_values(variables, allow_arrays=False) if variables is not None else {}
    for k in var_values:
        new_name(k)
    return placements, pin_specs, wire_specs, var_values


def populate_schematic(design: Any, plan: tuple[Any, ...]) -> None:
    if len(plan) == 2:
        placements, legacy_connections = plan
        pin_specs, var_values = [], {}
        wire_specs = [{"points": [list(pt) for pt in verts], "label": lbl} for verts, lbl in legacy_connections]
    else:
        placements, pin_specs, wire_specs, var_values = plan

    for name, master, at, angle, values in placements:
        instance = design.add_instance(master, at, name=name, angle=angle)
        if values:
            apply_instance_parameters(instance, values)

    if var_values:
        _set_var_block(design, "VAR1", var_values, at=(-4.0, -3.0))

    pin_locations: dict[str, tuple[float, float]] = {}
    for p_spec in pin_specs:
        _add_schematic_pin(design, p_spec, pin_locations)

    routed_segments: list[tuple[tuple[float, float], tuple[float, float]]] = []
    for w_spec in wire_specs:
        _add_schematic_wire(design, w_spec, pin_locations, routed_segments=routed_segments)


def apply_incremental_ops(workspace: Any, design: Any, ops: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Apply an ordered batch of schematic edit operations inside an active Transaction."""
    items = records(ops, 200, "ops")
    if not items:
        raise BridgeError("invalid_manifest", "Supply at least one incremental schematic operation.")

    pin_locations: dict[str, tuple[float, float]] = {}
    for term in design.terms:
        if getattr(term, "pins", ()):
            snap = term.pins[0].snap_point
            pin_locations[term.name] = (float(snap.x), float(snap.y))

    results: list[dict[str, Any]] = []
    for item in items:
        kind = str(item.get("op", "")).strip().lower()
        if kind in {"add_instance", "inst"}:
            # Accept both flat {"op":"add_instance","name":...} and nested {"op":"add_instance","instance":{"name":...}}
            payload = item.get("instance") if isinstance(item.get("instance"), dict) else item
            if not all(k in payload for k in ("name", "master", "at")):
                raise BridgeError("invalid_manifest", "add_instance requires 'name', 'master', and 'at'.")
            name = new_name(payload["name"])
            if name in design.instances:
                raise BridgeError("invalid_manifest", f"Instance '{name}' already exists.")
            master = normalize_symbol_lcv(payload["master"])
            if get_view(workspace, *master) is None:
                raise BridgeError("master_not_found", f"Cannot find {lcv_name(*master)}.")
            at = point(payload["at"])
            angle = number(payload.get("angle", 0.0))
            params = parameter_values(payload.get("parameters", {}))
            inst = design.add_instance(master, at, name=name, angle=angle)
            applied = apply_instance_parameters(inst, params) if params else []
            results.append({"op": "add_instance", "name": name, "master": lcv_name(*master), "parameters": applied})

        elif kind in {"add_wire", "wire"}:
            wired = _add_schematic_wire(design, item, pin_locations)
            results.append({"op": "add_wire", **wired})

        elif kind in {"add_pin", "pin"}:
            pinned = _add_schematic_pin(design, item, pin_locations)
            results.append({"op": "add_pin", **pinned})

        elif kind in {"set_var", "var"}:
            var_name = str(item.get("name", "VAR1"))
            at = point(item.get("at", [-4.0, -3.0]))
            updated = _set_var_block(design, var_name, item["variables"], at=at)
            results.append({"op": "set_var", **updated})

        elif kind in {"set_param", "param"}:
            inst_name = str(item.get("instance", ""))
            if inst_name not in design.instances:
                raise BridgeError("instance_not_found", f"Instance '{inst_name}' does not exist.")
            params = parameter_values(item["parameters"])
            applied = apply_instance_parameters(design.instances[inst_name], params)
            results.append({"op": "set_param", "instance": inst_name, "changes": applied})

        elif kind in {"delete_instance", "delete"}:
            inst_name = str(item.get("name") or item.get("instance") or "")
            if inst_name not in design.instances:
                raise BridgeError("instance_not_found", f"Instance '{inst_name}' does not exist.")
            design.instances[inst_name].delete_object()
            results.append({"op": "delete_instance", "name": inst_name})

        else:
            raise BridgeError(
                "invalid_manifest",
                f"Unsupported schematic op {item.get('op')!r}; expected add_instance, add_wire, add_pin, set_var, set_param, or delete_instance.",
            )
    return results


def check_schematic(target: Any, *, mode: str = "tb", search_dirs: list[str | Path] | None = None) -> dict[str, Any]:
    """Perform OpenAccess schematic connectivity + ADS semantic netlist checks."""
    if mode not in {"tb", "dut"}:
        raise BridgeError("invalid_argument", "Schematic check mode must be 'tb' or 'dut'.")

    diagnostics: list[dict[str, Any]] = []

    # 1. OpenAccess terminal / net degree inspection
    net_pin_counts: dict[str, int] = {}
    for term in target.terms:
        if term.net is not None:
            net_pin_counts[term.net.name] = net_pin_counts.get(term.net.name, 0) + 1

    for inst in target.instances:
        for it in inst.inst_terms:
            if it.net is not None:
                net_pin_counts[it.net.name] = net_pin_counts.get(it.net.name, 0) + 1

    for inst in target.instances:
        if bool(getattr(inst, "is_var_instance", False)):
            continue
        terms = list(inst.inst_terms)
        for it in terms:
            tid, _, _ = safe_inst_term_identity(it)
            if it.net is None:
                diagnostics.append({
                    "severity": "error",
                    "code": "unconnected_pin",
                    "message": f"Instance '{inst.name}' terminal '{tid}' is not connected to any net.",
                    "target": f"{inst.name}.{tid}",
                })
            elif not netlist_parser.is_ground_net(it.net.name) and net_pin_counts.get(it.net.name, 0) <= 1:
                diagnostics.append({
                    "severity": "error" if mode == "tb" else "warning",
                    "code": "dangling_pin",
                    "message": f"Instance '{inst.name}' terminal '{tid}' on net '{it.net.name}' has no peer connection.",
                    "target": f"{inst.name}.{tid}",
                })

    # 1b. Visual layout overlap check across non-ground instance bounding boxes
    inst_boxes: list[tuple[str, dict[str, float]]] = []
    for inst in target.instances:
        if bool(getattr(inst, "is_var_instance", False)):
            continue
        m_str = master_lcv_str(inst)
        if ":GROUND:" in m_str:
            continue
        try:
            b = box_values(inst.bbox)
            if b["right"] > b["left"] and b["top"] > b["bottom"]:
                inst_boxes.append((inst.name, b))
        except Exception:
            continue
    for i_idx in range(len(inst_boxes)):
        name_a, ba = inst_boxes[i_idx]
        area_a = (ba["right"] - ba["left"]) * (ba["top"] - ba["bottom"])
        for j_idx in range(i_idx + 1, len(inst_boxes)):
            name_b, bb = inst_boxes[j_idx]
            ix = min(ba["right"], bb["right"]) - max(ba["left"], bb["left"])
            iy = min(ba["top"], bb["top"]) - max(ba["bottom"], bb["bottom"])
            if ix > 0.05 and iy > 0.05:
                area_b = (bb["right"] - bb["left"]) * (bb["top"] - bb["bottom"])
                if (ix * iy) / max(min(area_a, area_b), 1e-9) >= 0.25:
                    diagnostics.append({
                        "severity": "warning",
                        "code": "overlapping_instances",
                        "message": f"Instances '{name_a}' and '{name_b}' bounding boxes overlap visually; consider increasing spacing.",
                        "target": f"{name_a},{name_b}",
                    })

    # 2. Netlist generation & semantic check
    revision: str | None = None
    ai_summary = ""
    try:
        netlist_text = target.generate_netlist()
        nl_report = netlist_parser.check_netlist(netlist_text, mode=mode, search_dirs=search_dirs)
        revision = nl_report.get("revision")
        ai_summary = nl_report.get("ai_summary", "")
        existing_keys = {(d["code"], d.get("target")) for d in diagnostics}
        for diag in nl_report["diagnostics"]:
            # Avoid duplicating floating_net when dangling_pin already pinpointed the terminal
            if diag["code"] == "floating_net":
                continue
            if (diag["code"], diag.get("target")) not in existing_keys:
                diagnostics.append(diag)
    except RuntimeError as exc:
        diagnostics.append({
            "severity": "error",
            "code": "netlist_generation_failed",
            "message": str(exc),
            "target": str(target.design_name),
        })

    errors = sum(1 for d in diagnostics if d["severity"] == "error")
    warnings = sum(1 for d in diagnostics if d["severity"] == "warning")
    return {
        "ok": errors == 0,
        "design": str(target.design_name),
        "mode": mode,
        "revision": revision,
        "error_count": errors,
        "warning_count": warnings,
        "diagnostics": diagnostics,
        "ai_summary": ai_summary,
    }


def layout_plan(shapes: list[dict[str, Any]]) -> list[tuple[str, int | str, list[tuple[float, float]]]]:
    plan = []
    for item in records(shapes, 600, "shapes"):
        kind = item["type"]
        layer = item["layer"]
        if not (type(layer) is int and 0 <= layer <= 65535 or isinstance(layer, str) and 0 < len(layer) <= 64):
            raise BridgeError("invalid_manifest", "Specify a layer name or a layer number between 0 and 65535.")
        vertices = points(item["points"], 3 if kind == "polygon" else 2)
        if kind == "rectangle":
            if len(vertices) != 2 or any(a >= b for a, b in zip(*vertices)):
                raise BridgeError("invalid_manifest", "A rectangle needs lower-left and upper-right points.")
        elif kind != "polygon":
            raise BridgeError("invalid_manifest", "Supported layout shapes are rectangle and polygon.")
        plan.append((kind, layer, vertices))
    return plan


def populate_layout(design: Any, plan: list[tuple[str, int | str, list[tuple[float, float]]]]) -> None:
    for kind, layer, vertices in plan:
        layer_id = design.create_layer_id(layer, "drawing") if isinstance(layer, str) else db_uu.LayerId(layer, -1)
        if kind == "rectangle":
            design.add_rectangle(layer_id, *vertices)
        else:
            design.add_polygon(layer_id, vertices)


def catalog(workspace: Any, library: str | None = None, search: str = "", offset: int = 0, limit: int = 100) -> dict[str, Any]:
    candidates = sorted(workspace.library_names)
    writable = set(workspace.writable_library_names)
    if type(limit) is not int or type(offset) is not int or not 1 <= limit <= 200 or offset < 0:
        raise BridgeError("invalid_argument", "Use nonnegative offset and limit from 1 through 200.")
    if not isinstance(search, str) or len(search) > 128:
        raise BridgeError("invalid_argument", "Search must be a string of at most 128 characters.")
    if library is None:
        matches: list[Any] = [
            {"name": name, "writable": name in writable}
            for name in candidates
            if search.casefold() in name.casefold()
        ]
    else:
        if library not in candidates:
            raise BridgeError("library_not_found", f"Unknown library {library}.")
        lib = workspace.open_library(library)
        matches = sorted(
            str(v.lcv_name)
            for cell in lib.cells
            for v in cell.views
            if search.casefold() in str(v.lcv_name).casefold()
        )
    return {"items": matches[offset:offset + limit], "total": len(matches), "offset": offset}


def create_library(workspace: Any, name: str, layout_technology: bool, record_state: Any) -> dict[str, Any]:
    name = new_name(name)
    if type(layout_technology) is not bool:
        raise BridgeError("invalid_argument", "layout_technology must be a boolean.")
    path = Path(workspace.path) / name
    if name in workspace.library_names or path.exists():
        raise BridgeError("library_exists", f"Library {name} or its directory already exists.")
    record_state("creating")
    library = de.create_new_library(name, path)
    workspace.add_library(name, path, de.LibraryMode.NON_SHARED)
    if layout_technology:
        library.create_layout_tech_std_ads("millimeter", 1000, True)
    return {"library": name, "path": str(path), "layout_technology": "ADS standard (mm)" if layout_technology else None}
