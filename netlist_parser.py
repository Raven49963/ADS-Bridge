"""Semantic parser, topology analyzer, and ERC checker for Keysight ADS netlists.

Designed so AI agents and Python automation scripts can read, validate, and reason
about real Keysight ADS (`hpeesofsim`) netlists generated from schematics or files
without guessing raw text syntax.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
import hashlib
import math
import operator
from pathlib import Path
import re
from typing import Any


GROUND_NETS = {"0", "gnd", "gnd!"}

SI_PREFIXES: dict[str, float] = {
    "f": 1e-15,
    "p": 1e-12,
    "n": 1e-9,
    "u": 1e-6,
    "\u00b5": 1e-6,  # micro sign
    "\u03bc": 1e-6,  # greek small letter mu
    "m": 1e-3,
    "": 1.0,
    "k": 1e3,
    "K": 1e3,
    "M": 1e6,
    "Meg": 1e6,
    "MEG": 1e6,
    "G": 1e9,
    "T": 1e12,
}

SOLVER_ANALYSIS_TYPES = {
    "AC", "S_Param", "SP", "DC", "Tran", "Transient", "HB", "HarmonicBalance",
    "Noise", "LSSP", "Env", "Envelope", "X_Param",
}
ANALYSIS_TYPES = SOLVER_ANALYSIS_TYPES | {"ParamSweep", "SweepPlan", "OutputPlan", "Optim", "Goal", "MeasEqn"}
PORT_TYPES = {"Port", "Term", "TermG"}
SOURCE_TYPES = {
    "V_Source", "I_Source", "V_AC", "V_DC", "V_1Tone", "V_nTone",
    "I_AC", "I_DC", "I_1Tone", "VtPulse", "VtSine", "VtStep", "ItPulse",
}
PASSIVE_TYPES = {"R", "C", "L", "TF", "Mutual", "Short", "RES", "CAP", "IND", "INDQ", "CAPQ"}
TLINE_TYPES = {"MLIN", "TLIN", "TLINP", "TLIN4", "CLIN", "MTEE", "MCORN", "MSTEP", "MSUB", "Subst"}
PROBE_TYPES = {"I_Probe", "NodeSet", "Conn"}
SUBSTRATE_OR_META_TYPES = {
    "MSUB", "Subst", "Options", "Var", "VAR", "MeasEqn",
    "SweepPlan", "OutputPlan", "ParamSweep", "Optim", "Goal",
}

_NUMBER_UNIT_RE = re.compile(
    r"^\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\s*(.*?)\s*$"
)
_UNIT_PATTERNS: dict[str, tuple[re.Pattern[str], str]] = {
    "resistance": (
        re.compile(r"^(f|p|n|u|\u00b5|\u03bc|m|k|K|Meg|MEG|M|G|T)?(?:[Oo]hm|OHM|[Oo]hms|OHMS|\u03a9)?$"),
        "Ohm",
    ),
    "capacitance": (
        re.compile(r"^(f|p|n|u|\u00b5|\u03bc|m|k|K|Meg|MEG|M|G|T)?[Ff]?$"),
        "F",
    ),
    "inductance": (
        re.compile(r"^(f|p|n|u|\u00b5|\u03bc|m|k|K|Meg|MEG|M|G|T)?[Hh]?$"),
        "H",
    ),
    "frequency": (
        re.compile(r"^(f|p|n|u|\u00b5|\u03bc|m|k|K|Meg|MEG|M|G|T)?(?:[Hh]z|HZ)?$"),
        "Hz",
    ),
    "time": (
        re.compile(r"^(f|p|n|u|\u00b5|\u03bc|m|k|K|Meg|MEG|M|G|T)?(?:s|sec|SEC)?$"),
        "s",
    ),
    "voltage": (
        re.compile(r"^(f|p|n|u|\u00b5|\u03bc|m|k|K|Meg|MEG|M|G|T)?[Vv]?$"),
        "V",
    ),
    "current": (
        re.compile(r"^(f|p|n|u|\u00b5|\u03bc|m|k|K|Meg|MEG|M|G|T)?[Aa]?$"),
        "A",
    ),
    "length": (
        re.compile(r"^(f|p|n|u|\u00b5|\u03bc|m|k|K|Meg|MEG|M|G|T)?(?:m|mil|inch|um|mm|nm)?$"),
        "m",
    ),
    "generic": (
        re.compile(r"^(f|p|n|u|\u00b5|\u03bc|m|k|K|Meg|MEG|M|G|T)?(?:[Oo]hm|OHM|[Oo]hms|OHMS|\u03a9|[FfHhVvAaSs]|[Hh]z|HZ|sec|m|um|mm|nm|mil)?$"),
        "",
    ),
}

_PARAM_KIND_HINTS: dict[tuple[str, str], str] = {
    ("R", "R"): "resistance",
    ("Port", "Z"): "resistance",
    ("Term", "Z"): "resistance",
    ("TermG", "Z"): "resistance",
    ("C", "C"): "capacitance",
    ("L", "L"): "inductance",
    ("AC", "Start"): "frequency",
    ("AC", "Stop"): "frequency",
    ("AC", "Step"): "frequency",
    ("S_Param", "Start"): "frequency",
    ("S_Param", "Stop"): "frequency",
    ("S_Param", "Step"): "frequency",
    ("HB", "Freq"): "frequency",
    ("Tran", "StartTime"): "time",
    ("Tran", "StopTime"): "time",
    ("Tran", "MaxTimeStep"): "time",
}
ERC_QUANTITY_PARAMETERS = {("R", "R"), ("C", "C"), ("L", "L"), ("Port", "Z"), ("Term", "Z")}

_IDENT_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\b")
_SAFE_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Pow: operator.pow,
}
_SAFE_UNARY_OPS = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}
_SAFE_FUNCS: dict[str, Any] = {
    "sqrt": math.sqrt,
    "log10": math.log10,
    "ln": math.log,
    "log": math.log,
    "exp": math.exp,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "abs": abs,
}


class NetlistError(ValueError):
    """Raised when an ADS netlist is syntactically malformed or fails strict review."""


@dataclass(frozen=True)
class ComponentStatement:
    model_type: str
    instance_name: str
    nodes: tuple[str, ...]
    params: dict[str, str]
    line_no: int = 0


@dataclass
class SubcircuitDef:
    name: str
    ports: tuple[str, ...]
    parameters: dict[str, str] = field(default_factory=dict)
    variables: dict[str, str] = field(default_factory=dict)
    instances: list[ComponentStatement] = field(default_factory=list)
    line_no: int = 0


def is_ground_net(net: str) -> bool:
    return net.strip().lower() in GROUND_NETS


def _strip_comment(line: str) -> str:
    """Strip ADS inline comments (; or //) outside quoted strings."""
    in_quotes = False
    i = 0
    n = len(line)
    while i < n:
        ch = line[i]
        if ch == '"':
            in_quotes = not in_quotes
            i += 1
            continue
        if not in_quotes:
            if ch == ";":
                return line[:i]
            if ch == "/" and i + 1 < n and line[i + 1] == "/":
                return line[:i]
        i += 1
    return line


def _extract_header_metadata(text: str) -> dict[str, str]:
    meta: dict[str, str] = {}
    for raw in text.splitlines()[:25]:
        stripped = raw.strip()
        if not stripped.startswith(";"):
            continue
        body = stripped.lstrip(";").strip()
        if body.startswith("Top Design:"):
            meta["top_design"] = body.split(":", 1)[1].strip().strip('"')
        elif body.startswith("Netlisted using Hierarchy Policy:"):
            meta["hierarchy_policy"] = body.split(":", 1)[1].strip().strip('"')
    return meta


def _join_logical_lines_with_lineno(text: str) -> list[tuple[int, str]]:
    """Strip comments and join backslash-continued lines while preserving start line numbers."""
    logical: list[tuple[int, str]] = []
    buffer = ""
    start_line = 1
    for idx, raw_line in enumerate(text.splitlines(), start=1):
        cleaned = _strip_comment(raw_line).strip()
        if not cleaned:
            if buffer:
                logical.append((start_line, buffer.strip()))
                buffer = ""
            continue
        if not buffer:
            start_line = idx
        if cleaned.endswith("\\"):
            buffer = f"{buffer} {cleaned[:-1].rstrip()}".strip()
        else:
            buffer = f"{buffer} {cleaned}".strip()
            logical.append((start_line, buffer))
            buffer = ""
    if buffer:
        logical.append((start_line, buffer.strip()))
    return logical


def _normalize_equals(line: str) -> str:
    """Remove whitespace around '=' outside quotes and parentheses."""
    out: list[str] = []
    in_quotes = False
    paren_depth = 0
    i = 0
    n = len(line)
    while i < n:
        ch = line[i]
        if ch == '"':
            in_quotes = not in_quotes
            out.append(ch)
            i += 1
        elif not in_quotes and ch == "(":
            paren_depth += 1
            out.append(ch)
            i += 1
        elif not in_quotes and ch == ")":
            paren_depth -= 1
            out.append(ch)
            i += 1
        elif not in_quotes and paren_depth == 0 and ch == "=":
            while out and out[-1].isspace():
                out.pop()
            out.append("=")
            i += 1
            while i < n and line[i].isspace():
                i += 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _tokenize_statement(line: str) -> list[str]:
    """Split a statement into tokens while preserving quoted strings and parentheses."""
    tokens: list[str] = []
    current: list[str] = []
    in_quotes = False
    paren_depth = 0

    for ch in line:
        if ch == '"':
            in_quotes = not in_quotes
            current.append(ch)
        elif not in_quotes and ch == "(":
            paren_depth += 1
            current.append(ch)
        elif not in_quotes and ch == ")":
            paren_depth -= 1
            if paren_depth < 0:
                raise NetlistError(f"Unbalanced parentheses in line: {line}")
            current.append(ch)
        elif not in_quotes and paren_depth == 0 and ch.isspace():
            if current:
                tokens.append("".join(current))
                current = []
        else:
            current.append(ch)

    if in_quotes:
        raise NetlistError(f"Unterminated quote in line: {line}")
    if paren_depth != 0:
        raise NetlistError(f"Unbalanced parentheses in line: {line}")
    if current:
        tokens.append("".join(current))
    return tokens


def _has_unquoted_equals(token: str) -> bool:
    in_quotes = False
    paren_depth = 0
    for ch in token:
        if ch == '"':
            in_quotes = not in_quotes
        elif not in_quotes and ch == "(":
            paren_depth += 1
        elif not in_quotes and ch == ")":
            paren_depth -= 1
        elif not in_quotes and paren_depth == 0 and ch == "=":
            return True
    return False


def _parse_kv_tokens(tokens: list[str], line: str) -> tuple[list[str], dict[str, str]]:
    """Split tokens into positional prefix tokens and trailing key=value parameters."""
    positional: list[str] = []
    params: dict[str, str] = {}
    current_param: str | None = None
    for tok in tokens:
        if _has_unquoted_equals(tok):
            key, val = tok.split("=", 1)
            if not key:
                raise NetlistError(f"Empty parameter name in line: {line}")
            params[key] = val
            current_param = key
        elif current_param is None:
            positional.append(tok)
        else:
            params[current_param] = f"{params[current_param]} {tok}".strip()
    return positional, params


_EXPLICIT_PARAM_UNIT_RE = re.compile(
    r"^(?:(?:f|p|n|u|\u00b5|\u03bc|m|k|K|Meg|MEG|M|G|T)?(?:[Oo]hm|OHM|[Oo]hms|OHMS|\u03a9|[FfHhVvAaSs]|[Hh]z|HZ|sec|m)|um|mm|nm|mil|inch)$"
)
_BARE_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _can_attach_parameter_unit(current_val: str, tok: str) -> bool:
    if not current_val or current_val.startswith('"') or not _EXPLICIT_PARAM_UNIT_RE.match(tok):
        return False
    num_match = _NUMBER_UNIT_RE.match(current_val)
    if num_match and num_match.group(2):
        try:
            parse_si_literal(current_val, "generic")
        except NetlistError:
            # Expression such as `2*R0`, not an inline unit literal; allow trailing unit token.
            return True
        return False
    return True


def _parse_parameters_tokens(tokens: list[str], line: str) -> dict[str, str]:
    """Parse ADS `parameters` declarations mixing `k=val [unit]` defaults and bare parameter names."""
    params: dict[str, str] = {}
    current_param: str | None = None
    awaiting_unit = False
    for tok in tokens:
        if _has_unquoted_equals(tok):
            key, val = tok.split("=", 1)
            if not key or not _BARE_IDENT_RE.match(key):
                raise NetlistError(f"Invalid parameter name {key!r} in line: {line}")
            params[key] = val
            current_param = key
            awaiting_unit = True
        elif current_param is not None and awaiting_unit and _can_attach_parameter_unit(params[current_param], tok):
            params[current_param] = f"{params[current_param]} {tok}".strip()
            awaiting_unit = False
        else:
            if not _BARE_IDENT_RE.match(tok):
                raise NetlistError(f"Malformed token {tok!r} in parameters declaration: {line}")
            params.setdefault(tok, "")
            current_param = None
            awaiting_unit = False
    return params


def parse_si_literal(raw: str, kind: str = "generic", *, require_positive: bool = False) -> float:
    """Parse a numeric literal with optional SI prefix and unit; raises NetlistError if not literal."""
    match = _NUMBER_UNIT_RE.match(raw)
    if not match:
        raise NetlistError(f"Unsupported {kind} expression or value: {raw!r}")
    num_str, unit_str = match.group(1), match.group(2).strip()
    value = float(num_str)

    if kind not in _UNIT_PATTERNS:
        raise NetlistError(f"Unknown quantity kind: {kind}")

    # Special lengths (mil, inch, um, mm, nm)
    if kind in {"length", "generic"} and unit_str in {"mil", "inch", "um", "mm", "nm"}:
        length_scales = {"nm": 1e-9, "um": 1e-6, "mm": 1e-3, "mil": 25.4e-6, "inch": 0.0254}
        result = value * length_scales[unit_str]
    else:
        pattern, _ = _UNIT_PATTERNS[kind]
        unit_match = pattern.match(unit_str)
        if not unit_match:
            raise NetlistError(f"Unsupported {kind} unit or expression: {raw!r}")
        prefix = unit_match.group(1) or ""
        if kind == "capacitance" and unit_str == "f":
            prefix = ""
        elif kind == "time" and unit_str == "s":
            prefix = ""
        elif kind in {"length", "generic"} and unit_str == "m":
            prefix = ""
        result = value * SI_PREFIXES[prefix]

    if not math.isfinite(result):
        raise NetlistError(f"{kind.capitalize()} must be finite: {raw!r}")
    if require_positive and result <= 0.0:
        raise NetlistError(f"{kind.capitalize()} must be positive: {raw!r}")
    return result


def _eval_ast_expr(node: ast.AST, env: dict[str, float]) -> float:
    if isinstance(node, ast.Expression):
        return _eval_ast_expr(node.body, env)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return float(node.value)
    if isinstance(node, ast.Name):
        if node.id == "pi":
            return math.pi
        if node.id == "e":
            return math.e
        if node.id in env:
            return env[node.id]
        raise KeyError(node.id)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _SAFE_UNARY_OPS:
        return float(_SAFE_UNARY_OPS[type(node.op)](_eval_ast_expr(node.operand, env)))
    if isinstance(node, ast.BinOp) and type(node.op) in _SAFE_BIN_OPS:
        left = _eval_ast_expr(node.left, env)
        right = _eval_ast_expr(node.right, env)
        return float(_SAFE_BIN_OPS[type(node.op)](left, right))
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _SAFE_FUNCS:
        args = [_eval_ast_expr(arg, env) for arg in node.args]
        return float(_SAFE_FUNCS[node.func.id](*args))
    raise ValueError("Unsupported expression AST")


def try_resolve_quantity(
    raw: str,
    variables: dict[str, float],
    kind: str = "generic",
) -> tuple[float | None, list[str]]:
    """Try resolving a literal or variable-backed expression to an SI float.

    Returns (resolved_float_or_None, unresolved_identifiers).
    """
    cleaned = raw.strip()
    if not cleaned or cleaned.startswith('"'):
        return None, []
    try:
        return parse_si_literal(cleaned, kind), []
    except NetlistError:
        pass

    # Strip trailing unit token if expression is followed by a unit, e.g. "2*R0 kOhm" or "R_val"
    parts = cleaned.rsplit(" ", 1)
    expr_str = cleaned
    unit_scale = 1.0
    if len(parts) == 2:
        try:
            unit_scale = parse_si_literal(f"1 {parts[1]}", kind)
            expr_str = parts[0].strip()
        except NetlistError:
            expr_str = cleaned

    # Replace ADS '^' exponentiation with '**'
    py_expr = expr_str.replace("^", "**")
    ads_builtins = {"pi", "e", "yes", "no", "true", "false", "polar", "db", "list", "freq", "time", "temp", "temper", "j"}
    referenced = [
        m.group(1)
        for m in _IDENT_RE.finditer(py_expr)
        if m.group(1) not in _SAFE_FUNCS and m.group(1) not in ads_builtins
    ]
    missing = [name for name in referenced if name not in variables]
    if missing:
        return None, sorted(set(missing))
    try:
        parsed = ast.parse(py_expr, mode="eval")
        val = _eval_ast_expr(parsed, variables) * unit_scale
        return (val if math.isfinite(val) else None), []
    except Exception:
        return None, []


def classify_component(model_type: str, subcircuit_names: set[str]) -> str:
    if model_type in ANALYSIS_TYPES:
        return "analysis"
    if model_type in PORT_TYPES:
        return "port"
    if model_type in SOURCE_TYPES:
        return "source"
    if model_type in PASSIVE_TYPES:
        return "passive"
    if model_type in TLINE_TYPES:
        return "tline"
    if model_type in PROBE_TYPES:
        return "probe"
    if model_type in subcircuit_names:
        return "subckt_instance"
    if model_type in {"Options", "Var", "VAR"}:
        return "options"
    return "device"


def _resolve_variables(raw_vars: dict[str, str]) -> tuple[dict[str, float], dict[str, list[str]]]:
    resolved: dict[str, float] = {}
    unresolved_deps: dict[str, list[str]] = {}
    pending = dict(raw_vars)
    for _ in range(len(pending) + 1):
        progressed = False
        for name, expr in list(pending.items()):
            val, missing = try_resolve_quantity(expr, resolved, "generic")
            if val is not None:
                resolved[name] = val
                pending.pop(name)
                unresolved_deps.pop(name, None)
                progressed = True
            else:
                unresolved_deps[name] = missing
        if not progressed:
            break
    return resolved, unresolved_deps


def _parse_component_line(line: str, line_no: int) -> ComponentStatement:
    normalized = _normalize_equals(line)
    tokens = _tokenize_statement(normalized)
    if not tokens:
        raise NetlistError("Empty statement")
    header = tokens[0]
    if ":" not in header:
        raise NetlistError(f"Unsupported or malformed statement at line {line_no}: {line}")
    model_type, instance_name = header.split(":", 1)
    rest_start = 1
    # ADS netlister emits space-separated headers for plan blocks, e.g. `SweepPlan: AC1_stim Start=1 Hz ...`
    if model_type and not instance_name and len(tokens) >= 2 and not _has_unquoted_equals(tokens[1]):
        instance_name = tokens[1]
        rest_start = 2
    if not model_type or not instance_name:
        raise NetlistError(f"Invalid component header at line {line_no}: {header}")
    nodes, params = _parse_kv_tokens(tokens[rest_start:], line)
    # Plan blocks (`SweepPlan`, `OutputPlan`) have no electrical circuit nodes; positional tokens are plan refs.
    if model_type in {"SweepPlan", "OutputPlan"}:
        nodes = []
    return ComponentStatement(
        model_type=model_type,
        instance_name=instance_name,
        nodes=tuple(nodes),
        params=params,
        line_no=line_no,
    )


def _extract_quoted_paths(line: str) -> list[str]:
    return re.findall(r'"([^"]+)"', line)


def _analyze_rc_filter(
    instances: list[ComponentStatement],
    resolved_vars: dict[str, float],
) -> dict[str, Any] | None:
    """Detect single-pole RC low-pass or high-pass filter and compute analytical response."""
    electrical = [
        c for c in instances
        if c.model_type not in ANALYSIS_TYPES and c.model_type not in {"Options", "Var", "VAR", "MeasEqn"}
    ]
    v_sources = [c for c in electrical if c.model_type in {"V_Source", "V_AC"}]
    resistors = [c for c in electrical if c.model_type == "R"]
    capacitors = [c for c in electrical if c.model_type == "C"]
    if len(electrical) != 3 or len(v_sources) != 1 or len(resistors) != 1 or len(capacitors) != 1:
        return None
    src, res, cap = v_sources[0], resistors[0], capacitors[0]
    if len(src.nodes) != 2 or len(res.nodes) != 2 or len(cap.nodes) != 2:
        return None
    if is_ground_net(src.nodes[0]) == is_ground_net(src.nodes[1]):
        return None
    input_net = src.nodes[1] if is_ground_net(src.nodes[0]) else src.nodes[0]
    r_val, _ = try_resolve_quantity(res.params.get("R", ""), resolved_vars, "resistance")
    c_val, _ = try_resolve_quantity(cap.params.get("C", ""), resolved_vars, "capacitance")
    if r_val is None or c_val is None or r_val <= 0 or c_val <= 0:
        return None

    tau = r_val * c_val
    fc = 1.0 / (2.0 * math.pi * tau)

    # Case 1: Low-pass (R in series input->out, C shunt out->gnd)
    if input_net in res.nodes:
        out_net = res.nodes[1] if res.nodes[0] == input_net else res.nodes[0]
        if not is_ground_net(out_net) and out_net in cap.nodes:
            cap_other = cap.nodes[1] if cap.nodes[0] == out_net else cap.nodes[0]
            if is_ground_net(cap_other):
                return {
                    "archetype": "rc_lowpass_1st_order",
                    "input_net": input_net,
                    "output_net": out_net,
                    "resistance_ohm": r_val,
                    "capacitance_f": c_val,
                    "time_constant_s": tau,
                    "cutoff_hz": fc,
                    "transfer_function": "H(j*2*pi*f) = 1 / (1 + j*2*pi*f*R*C)",
                }
    # Case 2: High-pass (C in series input->out, R shunt out->gnd)
    if input_net in cap.nodes:
        out_net = cap.nodes[1] if cap.nodes[0] == input_net else cap.nodes[0]
        if not is_ground_net(out_net) and out_net in res.nodes:
            res_other = res.nodes[1] if res.nodes[0] == out_net else res.nodes[0]
            if is_ground_net(res_other):
                return {
                    "archetype": "rc_highpass_1st_order",
                    "input_net": input_net,
                    "output_net": out_net,
                    "resistance_ohm": r_val,
                    "capacitance_f": c_val,
                    "time_constant_s": tau,
                    "cutoff_hz": fc,
                    "transfer_function": "H(j*2*pi*f) = (j*2*pi*f*R*C) / (1 + j*2*pi*f*R*C)",
                }
    return None


def _resolve_port_number(port: ComponentStatement, resolved_vars: dict[str, float]) -> int | None:
    raw = port.params.get("Num")
    if raw is None or not raw.strip():
        return None
    val, missing = try_resolve_quantity(raw, resolved_vars, "generic")
    if val is None or missing:
        return -1
    rounded = int(round(val))
    if rounded <= 0 or abs(val - rounded) > 1e-9:
        return -1
    return rounded


def _analyze_two_port_resistive(
    instances: list[ComponentStatement],
    resolved_vars: dict[str, float],
) -> dict[str, Any] | None:
    """Detect canonical 2-port series or shunt resistive S-parameter testbench."""
    electrical = [
        c for c in instances
        if c.model_type not in ANALYSIS_TYPES and c.model_type not in {"Options", "Var", "VAR", "MeasEqn"}
    ]
    ports = [c for c in electrical if c.model_type in PORT_TYPES]
    resistors = [c for c in electrical if c.model_type == "R"]
    if len(ports) != 2 or len(resistors) != 1 or len(electrical) != 3:
        return None

    nums = [_resolve_port_number(p, resolved_vars) for p in ports]
    if any(num is not None for num in nums):
        if set(nums) != {1, 2}:
            return None
        p1 = ports[0] if nums[0] == 1 else ports[1]
        p2 = ports[1] if nums[0] == 1 else ports[0]
    else:
        p1, p2 = ports[0], ports[1]

    res = resistors[0]
    if len(p1.nodes) < 2 or len(p2.nodes) < 2 or len(res.nodes) != 2:
        return None
    if not (is_ground_net(p1.nodes[1]) and is_ground_net(p2.nodes[1])):
        return None
    n1, n2 = p1.nodes[0], p2.nodes[0]
    z1, _ = try_resolve_quantity(p1.params.get("Z", "50 Ohm"), resolved_vars, "resistance")
    z2, _ = try_resolve_quantity(p2.params.get("Z", "50 Ohm"), resolved_vars, "resistance")
    r_val, _ = try_resolve_quantity(res.params.get("R", ""), resolved_vars, "resistance")
    if not z1 or not z2 or abs(z1 - z2) > 1e-9 or not r_val or r_val <= 0:
        return None
    z0 = z1
    if set(res.nodes) == {n1, n2} and n1 != n2:
        denom = r_val + 2.0 * z0
        s11 = r_val / denom
        s21 = (2.0 * z0) / denom
        return {
            "archetype": "two_port_series_resistor",
            "port1_net": n1,
            "port2_net": n2,
            "z0_ohm": z0,
            "resistance_ohm": r_val,
            "s11_expected": s11,
            "s21_expected": s21,
            "s21_db_expected": 20.0 * math.log10(abs(s21)),
        }
    return None


def inspect_netlist(
    text: str,
    *,
    search_dirs: list[str | Path] | None = None,
) -> dict[str, Any]:
    """Parse a Keysight ADS netlist into an AI-friendly structured AST, topology, and summary."""
    if not isinstance(text, str):
        raise NetlistError("Netlist text must be a non-empty string")
    text = text.lstrip("\ufeff")
    if not text.strip():
        raise NetlistError("Netlist text must be a non-empty string")

    header_meta = _extract_header_metadata(text)
    revision = hashlib.sha256(text.encode("utf-8")).hexdigest()

    directives: list[dict[str, Any]] = []
    options: dict[str, str] = {}
    models: list[dict[str, Any]] = []
    raw_variables: dict[str, str] = {}
    subcircuits: dict[str, SubcircuitDef] = {}
    top_instances: list[ComponentStatement] = []
    active_subckt: SubcircuitDef | None = None

    roots = [Path(p).resolve() for p in (search_dirs or []) if p]

    for line_no, line in _join_logical_lines_with_lineno(text):
        # 1. Preprocessor directives (#include, #uselib, #define, ...)
        if line.startswith("#"):
            parts = line.split(None, 1)
            directive = parts[0].lower()
            rest = parts[1] if len(parts) > 1 else ""
            quoted = _extract_quoted_paths(rest)
            entry: dict[str, Any] = {"directive": directive, "raw": line, "line": line_no, "paths": quoted}
            if directive == "#include" and quoted and roots:
                inc_path = Path(quoted[0])
                candidates = [inc_path] if inc_path.is_absolute() else [r / inc_path for r in roots]
                resolved = next((c.resolve() for c in candidates if c.is_file()), None)
                entry["resolved_path"] = str(resolved) if resolved else None
                entry["exists"] = resolved is not None
            directives.append(entry)
            continue

        # 2. Subcircuit define / end
        lower = line.lower()
        if lower.startswith("define ") or lower.startswith("define\t"):
            m = re.match(r"^define\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(([^)]*)\)", line, flags=re.IGNORECASE)
            if not m:
                raise NetlistError(f"Malformed subcircuit 'define' at line {line_no}: {line}")
            if active_subckt is not None:
                raise NetlistError(f"Nested subcircuit 'define' is unsupported at line {line_no}")
            ports = tuple(p for p in m.group(2).split() if p)
            active_subckt = SubcircuitDef(name=m.group(1), ports=ports, line_no=line_no)
            subcircuits[active_subckt.name] = active_subckt
            continue

        if lower == "end" or lower.startswith("end ") or lower.startswith("end\t"):
            if active_subckt is None:
                raise NetlistError(f"Unexpected 'end' outside subcircuit at line {line_no}")
            active_subckt = None
            continue

        # 3. Options statement without ':'
        normalized = _normalize_equals(line)
        tokens = _tokenize_statement(normalized)
        if not tokens:
            continue

        head = tokens[0]
        if head.lower() == "options":
            _, kv = _parse_kv_tokens(tokens[1:], line)
            options.update(kv)
            continue

        # 4. 'parameters' declaration inside or outside subcircuit
        if head.lower() == "parameters":
            parsed_params = _parse_parameters_tokens(tokens[1:], line)
            target_map = active_subckt.parameters if active_subckt is not None else raw_variables
            for k, v in parsed_params.items():
                if v or k not in target_map:
                    target_map[k] = v
            continue

        # 5. 'model' card: model <name> <type> key=val ...
        if head.lower() == "model":
            if len(tokens) < 3:
                raise NetlistError(f"Malformed model statement at line {line_no}: {line}")
            _, kv = _parse_kv_tokens(tokens[3:], line)
            models.append({"name": tokens[1], "model_type": tokens[2], "parameters": kv, "line": line_no})
            continue

        # 5b. ADS solver directives without ':' (e.g. `Component Module="LinearCollapse" ...`, `globalnode ...`)
        if ":" not in head and head.lower() in {"component", "globalnode", "ground"}:
            pos, kv = _parse_kv_tokens(tokens[1:], line)
            directives.append({
                "directive": head.lower(),
                "raw": line,
                "line": line_no,
                "args": pos,
                "parameters": kv,
            })
            continue

        # 6. Top-level or subcircuit variable assignment: R_val=3 kOhm
        if head.lower() == "global" and len(tokens) > 1 and _has_unquoted_equals(tokens[1]):
            _, kv = _parse_kv_tokens(tokens[1:], line)
            raw_variables.update(kv)
            continue
        if ":" not in head and _has_unquoted_equals(head):
            _, kv = _parse_kv_tokens(tokens, line)
            target_vars = active_subckt.variables if active_subckt is not None else raw_variables
            target_vars.update(kv)
            continue

        # 7. Standard ADS component/controller statement Type:InstanceName ...
        comp = _parse_component_line(line, line_no)
        if comp.model_type in {"Var", "VAR"}:
            target_vars = active_subckt.variables if active_subckt is not None else raw_variables
            target_vars.update(comp.params)
        elif comp.model_type == "Options":
            options.update(comp.params)
        if active_subckt is not None:
            active_subckt.instances.append(comp)
        else:
            top_instances.append(comp)

    if active_subckt is not None:
        raise NetlistError(f"Unclosed subcircuit '{active_subckt.name}' (started at line {active_subckt.line_no})")

    resolved_vars, unresolved_var_deps = _resolve_variables(raw_variables)
    subckt_names = set(subcircuits.keys())

    # Build structured instances & analyses & topology
    structured_instances: list[dict[str, Any]] = []
    analyses: list[dict[str, Any]] = []
    net_map: dict[str, list[dict[str, Any]]] = {}
    port_nets: set[str] = set()
    ground_nets_used: set[str] = set()

    for comp in top_instances:
        category = classify_component(comp.model_type, subckt_names)
        parsed_params: dict[str, Any] = {}
        for p_name, p_raw in comp.params.items():
            kind = _PARAM_KIND_HINTS.get((comp.model_type, p_name), "generic")
            si_val, missing = try_resolve_quantity(p_raw, resolved_vars, kind)
            entry_p: dict[str, Any] = {"raw": p_raw}
            if si_val is not None:
                entry_p["si_value"] = si_val
            if missing:
                entry_p["unresolved_identifiers"] = missing
            parsed_params[p_name] = entry_p

        if category == "analysis":
            sweep: dict[str, Any] = {
                "type": comp.model_type,
                "name": comp.instance_name,
                "parameters": comp.params,
                "line": comp.line_no,
            }
            for key, out_key, kind in (
                ("Start", "start_si", "frequency" if comp.model_type in {"AC", "S_Param", "SP"} else "generic"),
                ("Stop", "stop_si", "frequency" if comp.model_type in {"AC", "S_Param", "SP"} else "generic"),
                ("Step", "step_si", "frequency" if comp.model_type in {"AC", "S_Param", "SP"} else "generic"),
                ("StopTime", "stop_time_s", "time"),
                ("MaxTimeStep", "max_step_s", "time"),
            ):
                if key in comp.params and comp.params[key].strip():
                    val, _ = try_resolve_quantity(comp.params[key], resolved_vars, kind)
                    if val is not None:
                        sweep[out_key] = val
            analyses.append(sweep)

        # Record electrical pin connections
        for pin_idx, net_name in enumerate(comp.nodes, start=1):
            net_map.setdefault(net_name, []).append({
                "instance": comp.instance_name,
                "model_type": comp.model_type,
                "category": category,
                "pin_index": pin_idx,
            })
            if is_ground_net(net_name):
                ground_nets_used.add(net_name)
            elif category == "port":
                port_nets.add(net_name)

        structured_instances.append({
            "name": comp.instance_name,
            "model_type": comp.model_type,
            "category": category,
            "nodes": list(comp.nodes),
            "parameters": comp.params,
            "parsed_parameters": parsed_params,
            "line": comp.line_no,
        })

    nets_summary: list[dict[str, Any]] = []
    floating_nets: list[str] = []
    signal_nets: list[str] = []
    for net_name in sorted(net_map):
        conns = net_map[net_name]
        gnd = is_ground_net(net_name)
        is_port = net_name in port_nets
        if not gnd:
            signal_nets.append(net_name)
            if len(conns) == 1:
                floating_nets.append(net_name)
        nets_summary.append({
            "name": net_name,
            "is_ground": gnd,
            "is_port": is_port,
            "degree": len(conns),
            "connections": conns,
        })

    variables_summary = {
        k: {
            "raw": raw_variables[k],
            "si_value": resolved_vars.get(k),
            **({"unresolved": unresolved_var_deps[k]} if k in unresolved_var_deps else {}),
        }
        for k in sorted(raw_variables)
    }

    subckts_summary = [
        {
            "name": sc.name,
            "ports": list(sc.ports),
            "parameters": sc.parameters,
            "variables": sc.variables,
            "instance_count": len(sc.instances),
            "instances": [
                {"name": c.instance_name, "model_type": c.model_type, "nodes": list(c.nodes),
                 "parameters": c.params, "line": c.line_no}
                for c in sc.instances
            ],
        }
        for sc in subcircuits.values()
    ]

    insights: list[dict[str, Any]] = []
    rc_insight = _analyze_rc_filter(top_instances, resolved_vars)
    if rc_insight is not None:
        insights.append(rc_insight)
    sp_insight = _analyze_two_port_resistive(top_instances, resolved_vars)
    if sp_insight is not None:
        insights.append(sp_insight)

    # Compose concise human + AI readable summary
    elec_instances = [i for i in structured_instances if i["category"] not in {"analysis", "options"}]
    summary_parts = []
    if "top_design" in header_meta:
        summary_parts.append(f"Design `{header_meta['top_design']}`")
    summary_parts.append(
        f"{len(elec_instances)} circuit element(s), {len(signal_nets)} signal net(s), "
        f"{len(analyses)} analysis controller(s)"
    )
    if subckts_summary:
        summary_parts.append(f"{len(subckts_summary)} subcircuit definition(s)")
    if analyses:
        summary_parts.append("Analyses: " + ", ".join(f"{a['type']}:{a['name']}" for a in analyses))
    if insights:
        first = insights[0]
        if "cutoff_hz" in first:
            summary_parts.append(
                f"Detected {first['archetype']} ({first['input_net']} -> {first['output_net']}, "
                f"R={first['resistance_ohm']:.6g} Ohm, C={first['capacitance_f']:.6g} F, fc={first['cutoff_hz']:.6g} Hz)"
            )
        elif first["archetype"] == "two_port_series_resistor":
            summary_parts.append(
                f"Detected 2-port series resistor (R={first['resistance_ohm']:.6g} Ohm in Z0={first['z0_ohm']:.6g} Ohm: "
                f"S11={first['s11_expected']:.6g}, S21={first['s21_expected']:.6g})"
            )
    if floating_nets:
        summary_parts.append(f"Warning: floating net(s) {floating_nets}")

    return {
        "revision": revision,
        "metadata": header_meta,
        "options": options,
        "directives": directives,
        "models": models,
        "variables": variables_summary,
        "subcircuits": subckts_summary,
        "instances": structured_instances,
        "analyses": analyses,
        "topology": {
            "nets": nets_summary,
            "ground_nets": sorted(ground_nets_used),
            "port_nets": sorted(port_nets),
            "signal_nets": signal_nets,
            "floating_nets": floating_nets,
        },
        "circuit_insights": insights,
        "ai_summary": "; ".join(summary_parts) + ".",
    }


def _collect_included_symbols(parsed: dict[str, Any]) -> tuple[dict[str, float], set[str], bool]:
    """Read top-level variable declarations from resolved #include files and detect unresolved external includes."""
    included_raw: dict[str, str] = {}
    has_unexpanded_external = False
    for directive in parsed["directives"]:
        dtype = directive.get("directive")
        if dtype == "#uselib":
            has_unexpanded_external = True
            continue
        if dtype != "#include":
            continue
        if directive.get("exists") is False:
            continue
        resolved_path = directive.get("resolved_path")
        if not resolved_path:
            has_unexpanded_external = True
            continue
        try:
            inc_parsed = inspect_netlist(Path(resolved_path).read_text(encoding="utf-8-sig"))
        except Exception:
            has_unexpanded_external = True
            continue
        for var_name, var_info in inc_parsed.get("variables", {}).items():
            included_raw.setdefault(var_name, var_info.get("raw", ""))
        if any(d.get("directive") in {"#include", "#uselib"} for d in inc_parsed.get("directives", [])):
            has_unexpanded_external = True
    resolved_inc, _ = _resolve_variables(included_raw)
    return resolved_inc, set(included_raw.keys()), has_unexpanded_external


_SDD_EQUATION = re.compile(r"[IF]\[(\d+),\d+\]", re.IGNORECASE)


def _check_sdd_equations(instance: dict[str, Any], target: str) -> list[dict[str, Any]]:
    if instance["model_type"].upper() != "SDD":
        return []
    nodes = instance["nodes"]
    if not nodes or len(nodes) % 2:
        detail = "each port requires a pair of nodes"
    else:
        expected = set(range(1, len(nodes) // 2 + 1))
        declared = {
            int(match[1])
            for key, value in instance["parameters"].items()
            if value.strip() and (match := _SDD_EQUATION.fullmatch(key))
        }
        missing = sorted(expected - declared)
        extra = sorted(declared - expected)
        if not missing and not extra:
            return []
        parts = []
        if missing:
            parts.append(f"missing I or F equation for port(s) {missing}")
        if extra:
            parts.append(f"equation references port(s) outside the node pairs: {extra}")
        detail = ", ".join(parts)
    return [{
        "severity": "error",
        "code": "invalid_sdd_equations",
        "message": f"SDD {target}: {detail}.",
        "target": target,
        "line": instance.get("line"),
    }]


def _check_subcircuit_references(
    parsed: dict[str, Any],
    *,
    mode: str,
    extra_top_symbols: set[str],
    resolved_top_vars: dict[str, float],
    unresolved_severity: str,
) -> list[dict[str, Any]]:
    """Check subcircuit internals and hierarchical references across active call paths."""
    subcircuits = {sc["name"]: sc for sc in parsed["subcircuits"]}
    if not subcircuits:
        return []

    top_symbols = set(parsed["variables"]) | extra_top_symbols
    own_symbols = {
        name: top_symbols | sc["parameters"].keys() | sc["variables"].keys()
        for name, sc in subcircuits.items()
    }

    top_callees = {
        inst["model_type"]
        for inst in parsed["instances"]
        if inst["model_type"] in subcircuits
    }
    if mode == "tb" and top_callees:
        active_subckts: set[str] = set()
        queue = list(top_callees)
        while queue:
            cur = queue.pop()
            if cur in active_subckts or cur not in subcircuits:
                continue
            active_subckts.add(cur)
            for inst in subcircuits[cur]["instances"]:
                callee = inst["model_type"]
                if callee in subcircuits and callee not in active_subckts:
                    queue.append(callee)
    else:
        active_subckts = set(subcircuits.keys())

    callers: dict[str, set[str]] = {name: set() for name in subcircuits}
    for callee in top_callees:
        callers[callee].add("__top__")
    for caller_name in active_subckts:
        for inst in subcircuits[caller_name]["instances"]:
            callee = inst["model_type"]
            if callee in subcircuits and callee != caller_name:
                callers[callee].add(caller_name)

    # A caller symbol is guaranteed in scope inside `callee` only if EVERY active caller provides it.
    visible = {name: set(own_symbols[name]) for name in subcircuits}
    for _ in range(len(subcircuits) + 1):
        changed = False
        for name in subcircuits:
            active_callers = callers[name]
            if not active_callers:
                new_vis = set(own_symbols[name])
            else:
                caller_sets = [
                    top_symbols if caller == "__top__" else visible[caller]
                    for caller in active_callers
                ]
                new_vis = set(own_symbols[name]) | set.intersection(*caller_sets)
            if new_vis != visible[name]:
                visible[name] = new_vis
                changed = True
        if not changed:
            break

    diagnostics: list[dict[str, Any]] = []

    def check_reference(scope: str, target: str, raw: str, kind: str, code: str, line: int | None = None) -> None:
        _, referenced = try_resolve_quantity(raw, {}, kind)
        missing = sorted(set(referenced) - visible[scope])
        if missing:
            diagnostic: dict[str, Any] = {
                "severity": unresolved_severity,
                "code": code,
                "message": f"{target} = {raw!r} references name(s) not declared in the parsed scope: {missing}.",
                "target": target,
            }
            if line is not None:
                diagnostic["line"] = line
            diagnostics.append(diagnostic)

    for name, sc in subcircuits.items():
        local_raw = {k: v for k, v in {**sc["parameters"], **sc["variables"]}.items() if v.strip()}
        local_resolved, _ = _resolve_variables({
            **{k: str(v) for k, v in resolved_top_vars.items()},
            **local_raw,
        })
        for var, raw in {**sc["parameters"], **sc["variables"]}.items():
            check_reference(name, f"{name}.{var}", raw, "generic", "unresolved_variable")

        seen_inst_names: dict[str, str] = {}
        for instance in sc["instances"]:
            inst_name = instance["name"]
            model = instance["model_type"]
            nodes = instance["nodes"]
            line_no = instance.get("line")
            diagnostics.extend(_check_sdd_equations(instance, f"{name}.{inst_name}"))

            if inst_name in seen_inst_names:
                diagnostics.append({
                    "severity": "error",
                    "code": "duplicate_instance",
                    "message": (
                        f"Duplicate instance name '{inst_name}' inside subcircuit '{name}' "
                        f"({seen_inst_names[inst_name]} and {model})."
                    ),
                    "target": f"{name}.{inst_name}",
                    **({"line": line_no} if line_no is not None else {}),
                })
            seen_inst_names[inst_name] = model

            if len(nodes) >= 2 and len(set(nodes)) == 1:
                diagnostics.append({
                    "severity": "error",
                    "code": "shorted_instance",
                    "message": f"{model}:{inst_name} in '{name}' has all {len(nodes)} pins shorted to net '{nodes[0]}'.",
                    "target": f"{name}.{inst_name}",
                    **({"line": line_no} if line_no is not None else {}),
                })

            for param, raw in instance["parameters"].items():
                key = (model, param)
                kind = _PARAM_KIND_HINTS.get(key, "generic")
                is_subcircuit_parameter = model in subcircuits and param in subcircuits[model]["parameters"]
                if key in ERC_QUANTITY_PARAMETERS:
                    si_val, _ = try_resolve_quantity(raw, local_resolved, kind)
                    if si_val is not None and si_val <= 0:
                        diagnostics.append({
                            "severity": "error",
                            "code": "non_positive_parameter",
                            "message": f"{model}:{inst_name} in '{name}' parameter '{param}={raw}' must be positive.",
                            "target": f"{name}.{inst_name}.{param}",
                            **({"line": line_no} if line_no is not None else {}),
                        })
                if key in ERC_QUANTITY_PARAMETERS or is_subcircuit_parameter:
                    target = f"{name}.{inst_name}.{param}"
                    check_reference(name, target, raw, kind, "unresolved_parameter", line_no)
    return diagnostics


def check_netlist(
    text: str,
    *,
    mode: str = "tb",
    search_dirs: list[str | Path] | None = None,
) -> dict[str, Any]:
    """Run semantic electrical-rule checks on an ADS netlist in 'tb' or 'dut' mode."""
    if mode not in {"tb", "dut"}:
        raise ValueError("mode must be 'tb' or 'dut'")

    diagnostics: list[dict[str, Any]] = []
    try:
        parsed = inspect_netlist(text, search_dirs=search_dirs)
    except NetlistError as exc:
        return {
            "ok": False,
            "mode": mode,
            "error_count": 1,
            "warning_count": 0,
            "diagnostics": [{"severity": "error", "code": "syntax_error", "message": str(exc), "target": None}],
            "parsed": None,
        }

    inc_resolved_vars, inc_symbol_names, has_unexpanded_external = _collect_included_symbols(parsed)
    unresolved_severity = "warning" if has_unexpanded_external else "error"
    resolved_top_vars = {
        **inc_resolved_vars,
        **{k: info["si_value"] for k, info in parsed["variables"].items() if info.get("si_value") is not None},
    }
    known_top_symbols = set(parsed["variables"]) | inc_symbol_names
    subcircuits = {sc["name"]: sc for sc in parsed["subcircuits"]}

    # 1. Duplicate instance names at top level
    seen_names: dict[str, str] = {}
    for inst in parsed["instances"]:
        name = inst["name"]
        if name in seen_names:
            diagnostics.append({
                "severity": "error",
                "code": "duplicate_instance",
                "message": f"Duplicate instance name '{name}' ({seen_names[name]} and {inst['model_type']}).",
                "target": name,
            })
        seen_names[name] = inst["model_type"]

    # 2. Directive file resolution checks
    for directive in parsed["directives"]:
        if directive.get("exists") is False:
            diagnostics.append({
                "severity": "error",
                "code": "missing_include",
                "message": f"Included file not found: {directive['paths'][0]}",
                "target": directive["paths"][0],
            })

    # 3. Unresolved variables
    for var_name, var_info in parsed["variables"].items():
        missing_vars = sorted(set(var_info.get("unresolved") or []) - known_top_symbols)
        if missing_vars:
            diagnostics.append({
                "severity": unresolved_severity,
                "code": "unresolved_variable",
                "message": f"Variable '{var_name}' references undefined identifier(s): {missing_vars}",
                "target": var_name,
            })

    # 4. Component-level checks (shorted pins, duplicate port Num, non-positive R/C/L/Z, unresolved param identifiers)
    electrical_count = 0
    excitation_count = 0
    seen_port_nums: dict[int, str] = {}
    for inst in parsed["instances"]:
        cat = inst["category"]
        nodes = inst["nodes"]
        name = inst["name"]
        mtype = inst["model_type"]
        line_no = inst.get("line")
        diagnostics.extend(_check_sdd_equations(inst, name))
        if cat not in {"analysis", "options"} and mtype not in SUBSTRATE_OR_META_TYPES:
            electrical_count += 1
        if cat in {"source", "port"}:
            excitation_count += 1

        if mtype in PORT_TYPES and "Num" in inst["parameters"]:
            num_val, num_missing = try_resolve_quantity(inst["parameters"]["Num"], resolved_top_vars, "generic")
            if num_val is not None and not num_missing and abs(num_val - round(num_val)) <= 1e-9:
                pnum = int(round(num_val))
                if pnum in seen_port_nums:
                    diagnostics.append({
                        "severity": "error",
                        "code": "duplicate_port_number",
                        "message": f"{mtype}:{name} duplicates port Num={pnum} already assigned to '{seen_port_nums[pnum]}'.",
                        "target": f"{name}.Num",
                    })
                else:
                    seen_port_nums[pnum] = name

        if len(nodes) >= 2 and len(set(nodes)) == 1:
            diagnostics.append({
                "severity": "error",
                "code": "shorted_instance",
                "message": f"{mtype}:{name} has all {len(nodes)} pins shorted to net '{nodes[0]}'.",
                "target": name,
            })

        for p_name, p_info in inst["parsed_parameters"].items():
            is_subcircuit_parameter = mtype in subcircuits and p_name in subcircuits[mtype]["parameters"]
            if (mtype, p_name) in ERC_QUANTITY_PARAMETERS:
                kind = _PARAM_KIND_HINTS.get((mtype, p_name), "generic")
                si_val = p_info.get("si_value")
                if si_val is None and inc_resolved_vars:
                    si_val, _ = try_resolve_quantity(p_info["raw"], resolved_top_vars, kind)
                if si_val is not None and si_val <= 0:
                    diagnostics.append({
                        "severity": "error",
                        "code": "non_positive_parameter",
                        "message": f"{mtype}:{name} parameter '{p_name}={p_info['raw']}' must be positive.",
                        "target": f"{name}.{p_name}",
                    })
            if (mtype, p_name) in ERC_QUANTITY_PARAMETERS or is_subcircuit_parameter:
                missing_ids = sorted(set(p_info.get("unresolved_identifiers") or []) - known_top_symbols)
                if missing_ids:
                    diagnostics.append({
                        "severity": unresolved_severity,
                        "code": "unresolved_parameter",
                        "message": (
                            f"{mtype}:{name} parameter '{p_name}={p_info['raw']}' references "
                            f"undefined variable(s): {missing_ids}"
                        ),
                        "target": f"{name}.{p_name}",
                        **({"line": line_no} if line_no is not None else {}),
                    })

    diagnostics.extend(
        _check_subcircuit_references(
            parsed,
            mode=mode,
            extra_top_symbols=inc_symbol_names,
            resolved_top_vars=resolved_top_vars,
            unresolved_severity=unresolved_severity,
        )
    )

    # 5. Topology checks (floating nets, missing ground)
    for fnet in parsed["topology"]["floating_nets"]:
        net_entry = next(n for n in parsed["topology"]["nets"] if n["name"] == fnet)
        conn = net_entry["connections"][0]
        diagnostics.append({
            "severity": "warning" if mode == "dut" else "error",
            "code": "floating_net",
            "message": f"Net '{fnet}' has only 1 connection ({conn['model_type']}:{conn['instance']} pin {conn['pin_index']}).",
            "target": fnet,
        })

    if electrical_count > 0 and not parsed["topology"]["ground_nets"] and not parsed["subcircuits"]:
        diagnostics.append({
            "severity": "error" if mode == "tb" else "warning",
            "code": "missing_ground",
            "message": "Top-level circuit has no ground reference net ('0' or 'gnd').",
            "target": None,
        })

    # 6. Mode-specific checks ('tb' vs 'dut')
    solver_analyses = [analysis for analysis in parsed["analyses"] if analysis["type"] in SOLVER_ANALYSIS_TYPES]
    if mode == "tb":
        if electrical_count == 0:
            diagnostics.append({
                "severity": "error",
                "code": "empty_testbench",
                "message": "Testbench contains no electrical components.",
                "target": None,
            })
        if not solver_analyses:
            diagnostics.append({
                "severity": "error",
                "code": "missing_analysis",
                "message": "Testbench has no ADS simulation analysis controller (e.g. AC, S_Param, DC, Tran, HB).",
                "target": None,
            })
        if excitation_count == 0:
            diagnostics.append({
                "severity": "warning",
                "code": "missing_excitation",
                "message": "Testbench has no source (V_Source/I_Source) or termination port (Port/Term).",
                "target": None,
            })
    else:  # mode == "dut"
        if not parsed["subcircuits"] and electrical_count == 0:
            diagnostics.append({
                "severity": "error",
                "code": "empty_dut",
                "message": "DUT netlist defines neither subcircuits nor top-level devices.",
                "target": None,
            })
        if solver_analyses:
            diagnostics.append({
                "severity": "warning",
                "code": "embedded_analysis_in_dut",
                "message": "DUT netlist embeds simulation controllers; separate testbench from DUT if reusable.",
                "target": solver_analyses[0]["name"],
            })
        if excitation_count > 0:
            diagnostics.append({
                "severity": "warning",
                "code": "embedded_stimulus_in_dut",
                "message": "DUT netlist contains stimulus sources or termination ports.",
                "target": None,
            })

    errors = sum(1 for d in diagnostics if d["severity"] == "error")
    warnings = sum(1 for d in diagnostics if d["severity"] == "warning")
    return {
        "ok": errors == 0,
        "mode": mode,
        "revision": parsed["revision"],
        "error_count": errors,
        "warning_count": warnings,
        "diagnostics": diagnostics,
        "ai_summary": parsed["ai_summary"],
        "parsed": parsed,
    }


def review_rc(text: str, *, allow_options: bool = True) -> dict[str, str | float]:
    """Strict single-pole RC low-pass filter verifier for ADS netlists.

    Returns a JSON-serializable dict with keys:
      - input_net
      - output_net
      - resistance_ohm
      - capacitance_f
      - cutoff_hz
    """
    if not isinstance(text, str):
        raise NetlistError("Netlist text must be a non-empty string")
    text = text.lstrip("\ufeff")
    if not text.strip():
        raise NetlistError("Netlist text must be a non-empty string")

    components: list[ComponentStatement] = []
    seen_instances: set[tuple[str, str]] = set()
    for line_no, line in _join_logical_lines_with_lineno(text):
        normalized = _normalize_equals(line)
        tokens = _tokenize_statement(normalized)
        if not tokens:
            continue
        if allow_options and tokens[0].lower() == "options":
            continue
        comp = _parse_component_line(line, line_no)
        key = (comp.model_type, comp.instance_name)
        if key in seen_instances:
            raise NetlistError(f"Duplicate component instance: {comp.model_type}:{comp.instance_name}")
        seen_instances.add(key)
        components.append(comp)

    v_sources: list[ComponentStatement] = []
    resistors: list[ComponentStatement] = []
    capacitors: list[ComponentStatement] = []

    for comp in components:
        if comp.model_type in {"V_Source", "V_AC"}:
            v_sources.append(comp)
        elif comp.model_type == "R":
            resistors.append(comp)
        elif comp.model_type == "C":
            capacitors.append(comp)
        elif comp.model_type == "AC":
            continue
        elif allow_options and comp.model_type == "Options":
            continue
        else:
            raise NetlistError(
                f"Unsupported component affecting response: {comp.model_type}:{comp.instance_name}"
            )

    if len(v_sources) != 1:
        raise NetlistError(f"Expected exactly 1 V_Source, found {len(v_sources)}")
    if len(resistors) != 1:
        raise NetlistError(f"Expected exactly 1 resistor R, found {len(resistors)}")
    if len(capacitors) != 1:
        raise NetlistError(f"Expected exactly 1 capacitor C, found {len(capacitors)}")

    src, res, cap = v_sources[0], resistors[0], capacitors[0]

    if len(src.nodes) != 2:
        raise NetlistError(f"{src.model_type}:{src.instance_name} must have 2 pins, found {len(src.nodes)}")
    if src.nodes[0] == src.nodes[1]:
        raise NetlistError(f"{src.model_type}:{src.instance_name} pins are shorted to {src.nodes[0]}")
    src_type = src.params.get("Type")
    if src_type is not None and src_type.strip().strip('"') != "V_AC":
        raise NetlistError(f"Unsupported V_Source Type: {src_type}")
    if is_ground_net(src.nodes[0]) == is_ground_net(src.nodes[1]):
        raise NetlistError(
            f"{src.model_type}:{src.instance_name} must connect exactly one pin to ground, got {src.nodes}"
        )
    input_net = src.nodes[1] if is_ground_net(src.nodes[0]) else src.nodes[0]

    if len(res.nodes) != 2 or res.nodes[0] == res.nodes[1]:
        raise NetlistError(f"Invalid resistor connectivity on R:{res.instance_name}: {res.nodes}")
    if "R" not in res.params:
        raise NetlistError(f"R:{res.instance_name} is missing parameter 'R'")
    resistance_ohm = parse_si_literal(res.params["R"], "resistance", require_positive=True)

    if len(cap.nodes) != 2 or cap.nodes[0] == cap.nodes[1]:
        raise NetlistError(f"Invalid capacitor connectivity on C:{cap.instance_name}: {cap.nodes}")
    if "C" not in cap.params:
        raise NetlistError(f"C:{cap.instance_name} is missing parameter 'C'")
    capacitance_f = parse_si_literal(cap.params["C"], "capacitance", require_positive=True)

    if input_net not in res.nodes:
        raise NetlistError(
            f"Unsupported topology: resistor R:{res.instance_name} {res.nodes} is not connected to input_net {input_net!r}"
        )
    output_net = res.nodes[1] if res.nodes[0] == input_net else res.nodes[0]
    if is_ground_net(output_net):
        raise NetlistError(
            f"Unsupported topology: resistor R:{res.instance_name} is connected directly to ground"
        )

    cap_has_output = output_net in cap.nodes
    cap_other = cap.nodes[1] if cap.nodes[0] == output_net else cap.nodes[0]
    if not (cap_has_output and is_ground_net(cap_other)):
        raise NetlistError(
            f"Unsupported topology: capacitor C:{cap.instance_name} {cap.nodes} must connect {output_net!r} to ground"
        )

    cutoff_hz = 1.0 / (2.0 * math.pi * resistance_ohm * capacitance_f)
    return {
        "input_net": input_net,
        "output_net": output_net,
        "resistance_ohm": resistance_ohm,
        "capacitance_f": capacitance_f,
        "cutoff_hz": cutoff_hz,
    }


_BOILERPLATE_PARAMS = {
    "Noise", "Mode", "SaveCurrent", "AllowScaling", "Gdsnoi", "StatusLevel",
    "HBSS_WSP", "OutputPlan", "UseNutmegFormat", "ResourceUsage", "EnableOptim",
    "DcopOutputNodeVoltages", "DcopOutputPinCurrents", "DcopOutputAllSweepPoints",
    "DcopOutputDcopType", "LimitStepForTL", "TimeStepControl", "TruncTol",
    "ChargeTol", "IntegMethod", "MaxGearOrder", "Mu", "HB_Window", "HB_Sol",
    "ImpApprox", "ShortTL_Delay", "ImpMode", "UseInitCond", "LoadGminDC",
    "CheckKCL", "CheckOnlyDeltaV", "OverloadAlert", "DeviceBypass", "MaxIters",
    "MaxIter", "MaxItersDC", "DevOpPtLevel", "OutputAllPoints", "NoiseScale",
    "ImpEnforcePassivity", "ImpSkipTotalEnergy", "ImpLFEOn",
    "UseKrylov", "KrylovRestartLength", "KrylovDim", "KrylovTol",
    "AutoSelectSolver", "SamplFreq", "SteadyState", "UseSweepPlan",
    "Restart", "SaveSolns", "UseAllSS", "MatrixSolver", "FundOversample",
    "BlockName", "UseEquationNestLevel", "EquationNestLevel",
    "UseSavedEquationNestLevel", "SavedEquationNestLevel",
}

_TUNABLE_COMPONENT_PARAMS: dict[str, tuple[tuple[str, str], ...]] = {
    "R": (("R", "resistance"),),
    "RES": (("R", "resistance"),),
    "C": (("C", "capacitance"),),
    "CAP": (("C", "capacitance"),),
    "L": (("L", "inductance"), ("R", "resistance")),
    "IND": (("L", "inductance"), ("R", "resistance")),
    "Port": (("Z", "resistance"),),
    "Term": (("Z", "resistance"),),
    "TermG": (("Z", "resistance"),),
    "V_Source": (("Vdc", "voltage"),),
    "V_DC": (("Vdc", "voltage"),),
    "MLIN": (("W", "length"), ("L", "length")),
    "TLIN": (("Z", "resistance"), ("E", "generic")),
    "TLINP": (("Z", "resistance"), ("L", "length")),
}


_DEFAULT_IGNORABLE_PARAMS: dict[str, set[str]] = {
    "Temp": {"25", "25.0", "25 C", "25.0 C"},
    "Tnom": {"25", "25.0", "25 C", "25.0 C"},
    "Trise": {"0", "0.0"},
    "TC1": {"0", "0.0"},
    "TC2": {"0", "0.0"},
    "Mode": {"1"},
    "CalcS": {"yes"},
    "CalcY": {"no"},
    "CalcZ": {"no"},
    "CalcNoise": {"no"},
    "FreqConversion": {"no"},
    "FreqConversionPort": {"1"},
    "SortNoise": {"0"},
    "DevOpPtLevel": {"0"},
    "GroupDelayAperture": {"1e-4"},
    "BandwidthForNoise": {"1.0 Hz"},
}


def _is_ignorable_param(key: str, val: str) -> bool:
    s = val.strip()
    if not s or key in _BOILERPLATE_PARAMS:
        return True
    defaults = _DEFAULT_IGNORABLE_PARAMS.get(key)
    return defaults is not None and s in defaults


def _extract_unit_token(raw: str) -> str:
    cleaned = raw.strip()
    m = _NUMBER_UNIT_RE.match(cleaned)
    if m:
        return m.group(2).strip()
    parts = cleaned.rsplit(" ", 1)
    if len(parts) == 2 and _EXPLICIT_PARAM_UNIT_RE.match(parts[1]):
        return parts[1]
    return ""


def _infer_var_kind(raw: str, name: str) -> str:
    unit = _extract_unit_token(raw).lower()
    if "ohm" in unit or "\u03a9" in unit:
        return "resistance"
    if unit.endswith("hz"):
        return "frequency"
    if unit.endswith("f"):
        return "capacitance"
    if unit.endswith("h"):
        return "inductance"
    if unit.endswith("v"):
        return "voltage"
    if unit in {"m", "mm", "um", "nm", "mil", "inch"}:
        return "length"
    if unit.endswith("s") or unit == "sec":
        return "time"
    low = name.lower()
    if low.startswith(("r_", "rval", "res")):
        return "resistance"
    if low.startswith(("c_", "cval", "cap", "ce")):
        return "capacitance"
    if low.startswith(("l_", "lval", "ind")):
        return "inductance"
    if low.startswith(("vds", "vgs", "vdd", "vin")):
        return "voltage"
    return "generic"


def compress_netlist(
    text: str,
    *,
    search_dirs: list[str | Path] | None = None,
    include_models: bool = True,
    include_subcircuits: bool = True,
    format: str = "json",
) -> dict[str, Any]:
    """Compress a verbose ADS netlist into a compact LLM-friendly circuit-parameter representation.

    format:
      - 'json' (default): structured object with vars, components, analyses, and tunable_params.
      - 'tabular': ultra-compact tuple-row representation eliminating repeated JSON keys (~5-15x smaller than inspect).
    """
    fmt = format.strip().lower()
    if fmt not in {"json", "tabular"}:
        raise NetlistError(f"Unsupported compress format {format!r}; use 'json' or 'tabular'.")
    parsed = inspect_netlist(text, search_dirs=search_dirs)

    # Compact variables (drop unused default ADS VAR stub X=1.0 if unreferenced)
    referenced_names: set[str] = set()
    for inst in parsed["instances"]:
        for p_info in inst["parsed_parameters"].values():
            raw = p_info.get("raw", "")
            for m in _IDENT_RE.finditer(raw):
                referenced_names.add(m.group(1))
    for v_info in parsed["variables"].values():
        for m in _IDENT_RE.finditer(v_info.get("raw", "")):
            referenced_names.add(m.group(1))

    compact_vars: dict[str, str] = {}
    tunable_params: list[dict[str, Any]] = []
    for var_name, v_info in parsed["variables"].items():
        raw = v_info["raw"].strip()
        if var_name == "X" and raw in {"1", "1.0"} and "X" not in referenced_names:
            continue
        compact_vars[var_name] = raw
        if v_info.get("si_value") is not None:
            tunable_params.append({
                "ref": f"VAR:{var_name}",
                "kind": _infer_var_kind(raw, var_name),
                "raw": raw,
                "si": v_info["si_value"],
                "unit": _extract_unit_token(raw),
            })

    compact_components: list[dict[str, Any]] = []
    compact_analyses: list[dict[str, Any]] = []
    sweep_plans: dict[str, dict[str, str]] = {
        inst["name"]: {k: v for k, v in inst["parameters"].items() if v.strip()}
        for inst in parsed["instances"]
        if inst["model_type"].strip('"') == "SweepPlan"
    }
    for inst in parsed["instances"]:
        mtype = inst["model_type"].strip('"')
        cat = inst["category"]
        if mtype in {"Options", "OutputPlan", "Var", "VAR", "SweepPlan"}:
            continue
        if mtype == "Tran" and inst["name"].endswith("_tran") and inst["parameters"].get("HB_Sol") == "1":
            # Auto-generated ATAHB helper transient controller from HarmonicBalance symbol
            continue

        clean_params = {
            k: v
            for k, v in inst["parameters"].items()
            if not _is_ignorable_param(k, v)
        }
        if mtype == "Port" and "P[1]" in clean_params and "Pac" in clean_params:
            clean_params.pop("Pac", None)

        if cat == "analysis":
            sp_name = clean_params.pop("SweepPlan", "").strip('"')
            a_entry: dict[str, Any] = {"id": inst["name"], "type": mtype, "params": clean_params}
            if sp_name and sp_name in sweep_plans:
                a_entry["sweep_plan"] = sweep_plans[sp_name]
            compact_analyses.append(a_entry)
            continue

        comp_entry: dict[str, Any] = {
            "id": inst["name"],
            "type": mtype,
            "cat": cat,
            "nodes": inst["nodes"],
        }
        if clean_params:
            comp_entry["params"] = clean_params
        compact_components.append(comp_entry)

        # Register directly numeric instance parameters (skip those driven by a VAR already listed)
        for p_name, kind in _TUNABLE_COMPONENT_PARAMS.get(mtype, ()):
            if p_name not in clean_params:
                continue
            p_raw = clean_params[p_name]
            try:
                si_val = parse_si_literal(p_raw, kind)
            except NetlistError:
                continue
            tunable_params.append({
                "ref": f"{inst['name']}.{p_name}",
                "kind": kind,
                "raw": p_raw,
                "si": si_val,
                "unit": _extract_unit_token(p_raw),
            })

    result: dict[str, Any] = {
        "schema": "ads_bridge.netlist_compact.v1",
        "design": parsed["metadata"].get("top_design"),
        "revision": parsed["revision"],
        "summary": parsed["ai_summary"],
        "vars": compact_vars,
        "components": compact_components,
        "analyses": compact_analyses,
        "tunable_params": tunable_params,
        "ports": parsed["topology"]["port_nets"],
        "floating_nets": parsed["topology"]["floating_nets"],
    }
    if include_models and parsed["models"]:
        result["models"] = [
            {
                "name": m["name"],
                "type": m["model_type"],
                "params": {k: v for k, v in m["parameters"].items() if v.strip() and k not in _BOILERPLATE_PARAMS},
            }
            for m in parsed["models"]
        ]
    if include_subcircuits and parsed["subcircuits"]:
        result["subcircuits"] = [
            {
                "name": sc["name"],
                "ports": sc["ports"],
                "parameters": sc["parameters"],
                "instance_count": sc["instance_count"],
            }
            for sc in parsed["subcircuits"]
        ]
    if parsed["circuit_insights"]:
        result["insights"] = parsed["circuit_insights"]

    import json as _json
    raw_chars = len(text)
    inspect_chars = len(_json.dumps(parsed, ensure_ascii=False, separators=(",", ":")))

    if fmt == "tabular":
        topo_rows = [
            [
                c["id"],
                c["type"],
                " ".join(c["nodes"]),
                " ".join(f"{k}={v}" for k, v in c.get("params", {}).items()),
            ]
            for c in compact_components
        ]
        analysis_rows = []
        for a in compact_analyses:
            p_items = [f"{k}={v}" for k, v in a.get("params", {}).items()]
            if "sweep_plan" in a:
                p_items.extend(f"{k}={v}" for k, v in a["sweep_plan"].items())
            analysis_rows.append([a["id"], a["type"], " ".join(p_items)])
        param_rows: list[list[Any]] = []
        seen_p: set[str] = set()
        for tp in tunable_params:
            seen_p.add(tp["ref"])
            param_rows.append([tp["ref"], tp["raw"]])
        for vname, vraw in compact_vars.items():
            vref = f"VAR:{vname}"
            if vref not in seen_p:
                param_rows.append([vref, vraw])

        tab_out: dict[str, Any] = {
            "schema": "ads_bridge.netlist_tabular.v2",
            "rev": parsed["revision"][:12],
            "topo_cols": ["id", "type", "nodes", "params"],
            "topo": topo_rows,
            "analyses": analysis_rows,
            "params": param_rows,
        }
        if parsed["metadata"].get("top_design"):
            tab_out["design"] = parsed["metadata"]["top_design"]
        if parsed["topology"]["port_nets"]:
            tab_out["ports"] = parsed["topology"]["port_nets"]
        if parsed["topology"]["floating_nets"]:
            tab_out["floating"] = parsed["topology"]["floating_nets"]
        if include_models and parsed["models"]:
            tab_out["models"] = [[m["name"], m["model_type"], len(m["parameters"])] for m in parsed["models"]]
        if include_subcircuits and parsed["subcircuits"]:
            tab_out["subckts"] = [[sc["name"], " ".join(sc["ports"]), sc["instance_count"]] for sc in parsed["subcircuits"]]
        if parsed["circuit_insights"]:
            tab_out["insights"] = parsed["circuit_insights"]
        compact_chars = len(_json.dumps(tab_out, ensure_ascii=False, separators=(",", ":")))
        tab_out["stats"] = {
            "raw_chars": raw_chars,
            "inspect_chars": inspect_chars,
            "compact_chars": compact_chars,
            "compression_ratio": round(raw_chars / max(1, compact_chars), 2),
            "vs_inspect_ratio": round(inspect_chars / max(1, compact_chars), 2),
            "element_count": len(compact_components),
            "net_count": len(parsed["topology"]["signal_nets"]),
            "tunable_count": len(tunable_params),
        }
        return tab_out

    compact_chars = len(_json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    result["stats"] = {
        "raw_chars": raw_chars,
        "inspect_chars": inspect_chars,
        "compact_chars": compact_chars,
        "compression_ratio": round(raw_chars / max(1, compact_chars), 2),
        "vs_inspect_ratio": round(inspect_chars / max(1, compact_chars), 2),
        "element_count": len(compact_components),
        "net_count": len(parsed["topology"]["signal_nets"]),
        "tunable_count": len(tunable_params),
    }
    return result


def diff_netlist_params(old_text: str, new_text: str) -> dict[str, Any]:
    """Return a compact parameter-only delta between two versions of an ADS netlist."""
    old_c = compress_netlist(old_text, include_models=False, include_subcircuits=False, format="json")
    new_c = compress_netlist(new_text, include_models=False, include_subcircuits=False, format="json")

    def _param_map(comp: dict[str, Any]) -> dict[str, str]:
        pmap = {f"VAR:{k}": str(v) for k, v in comp.get("vars", {}).items()}
        for tp in comp.get("tunable_params", ()):
            pmap[tp["ref"]] = str(tp["raw"])
        return pmap

    m_old = _param_map(old_c)
    m_new = _param_map(new_c)
    changed: dict[str, list[str]] = {}
    added: dict[str, str] = {}
    removed: list[str] = []
    unchanged = 0

    for k, nv in m_new.items():
        if k not in m_old:
            added[k] = nv
        elif m_old[k] != nv:
            changed[k] = [m_old[k], nv]
        else:
            unchanged += 1
    for k in m_old:
        if k not in m_new:
            removed.append(k)

    out: dict[str, Any] = {
        "old_rev": old_c["revision"][:12],
        "new_rev": new_c["revision"][:12],
        "changed": changed,
        "unchanged_count": unchanged,
    }
    if added:
        out["added"] = added
    if removed:
        out["removed"] = sorted(removed)
    return out


def _format_param_assignment(value: str | float | int, unit: str = "") -> str:
    if isinstance(value, str):
        return value.strip()
    num_str = f"{float(value):.8g}"
    return f"{num_str} {unit}".strip() if unit else num_str


def _split_code_and_comment(raw_line: str) -> tuple[str, str]:
    """Split a physical ADS netlist line into `(code_part, comment_part)` respecting quoted strings."""
    in_quote = False
    i = 0
    n = len(raw_line)
    while i < n:
        ch = raw_line[i]
        if ch == '"' and (i == 0 or raw_line[i - 1] != "\\"):
            in_quote = not in_quote
        elif not in_quote:
            if ch == ";":
                return raw_line[:i], raw_line[i:]
            if ch == "/" and i + 1 < n and raw_line[i + 1] == "/":
                return raw_line[:i], raw_line[i:]
        i += 1
    return raw_line, ""


def _group_statement_lines(lines: list[str]) -> list[list[str]]:
    """Group physical lines into logical statements respecting ADS trailing-backslash `\\` continuations."""
    groups: list[list[str]] = []
    buf: list[str] = []
    for raw_line in lines:
        code, _ = _split_code_and_comment(raw_line)
        stripped = code.rstrip()
        buf.append(raw_line)
        if stripped.endswith("\\"):
            continue
        groups.append(buf)
        buf = []
    if buf:
        groups.append(buf)
    return groups


def apply_netlist_parameters(text: str, updates: dict[str, str | float | int]) -> str:
    """Patch top-level variables (`VAR:Name` or `Name`) and instance parameters (`Inst.Param`) in an ADS netlist.

    Handles ADS multi-line `\\` continuations, never modifies comments, and re-parses to confirm updates.
    Raises `NetlistError` if any target cannot be located in executable netlist code.
    """
    if not updates:
        return text
    lines = text.lstrip("\ufeff").splitlines()
    applied: set[str] = set()

    var_updates: dict[str, str] = {}
    inst_updates: dict[tuple[str, str], str] = {}
    for ref, val in updates.items():
        if not isinstance(ref, str) or not ref.strip():
            raise NetlistError(f"Invalid parameter reference: {ref!r}")
        ref_clean = ref.strip()
        val_str = _format_param_assignment(val)
        if ref_clean.upper().startswith("VAR:"):
            vname = ref_clean.split(":", 1)[1].strip()
            var_updates[vname] = val_str
        elif "." in ref_clean:
            inst_name, p_name = ref_clean.split(".", 1)
            inst_updates[(inst_name.strip(), p_name.strip())] = val_str
        else:
            var_updates[ref_clean] = val_str

    def _replace_param_in_code(raw_line: str, param_name: str, new_val: str) -> tuple[str, bool]:
        code_part, comment_part = _split_code_and_comment(raw_line)
        # Preserve trailing backslash continuation token if present
        trailing_bs = ""
        code_body = code_part
        m_bs = re.search(r"(\s*\\\s*)$", code_part)
        if m_bs:
            trailing_bs = m_bs.group(1)
            code_body = code_part[:m_bs.start()]
        pat = re.compile(
            rf"(?<![A-Za-z0-9_])({re.escape(param_name)})\s*=\s*"
            r'("[^"]*"|[^\s;\\]+(?:\s+(?:(?:f|p|n|u|\u00b5|\u03bc|m|k|K|Meg|MEG|M|G|T)?(?:[Oo]hm|OHM|[Oo]hms|OHMS|\u03a9|[FfHhVvAaSs]|[Hh]z|HZ|sec|m)|um|mm|nm|mil|inch)(?=\s|$|;|\\))?)'
        )
        m = pat.search(code_body)
        if not m:
            return raw_line, False
        new_code = code_body[:m.start()] + f"{param_name}={new_val}" + code_body[m.end():]
        return new_code + trailing_bs + comment_part, True

    out_lines: list[str] = []
    in_subckt = False
    for stmt_lines in _group_statement_lines(lines):
        joined_code = " ".join(
            _split_code_and_comment(ln)[0].rstrip().rstrip("\\").strip()
            for ln in stmt_lines
        ).strip()
        low = joined_code.lower()
        if low.startswith("define ") or low.startswith("define\t"):
            in_subckt = True
            out_lines.extend(stmt_lines)
            continue
        if low == "end" or low.startswith("end ") or low.startswith("end\t"):
            in_subckt = False
            out_lines.extend(stmt_lines)
            continue
        if in_subckt or not joined_code or joined_code.startswith("#"):
            out_lines.extend(stmt_lines)
            continue

        # 1. Top-level variable assignment `name = value` (single-line or continued)
        first_code, first_comment = _split_code_and_comment(stmt_lines[0])
        var_m = re.match(r"^(\s*(?:global\s+)?)([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+?)\s*$", first_code)
        if len(stmt_lines) == 1 and var_m and ":" not in var_m.group(2) and var_m.group(2) in var_updates:
            vname = var_m.group(2)
            out_lines.append(f"{var_m.group(1)}{vname}={var_updates[vname]}{first_comment}")
            applied.add(f"VAR:{vname}")
            continue

        # 2. Component statement `Model:InstName ...` across all continuation lines
        comp_m = re.match(r'^\s*("[^"]+"|[A-Za-z_][A-Za-z0-9_]*)\s*:\s*([A-Za-z_][A-Za-z0-9_]*)\b', joined_code)
        if comp_m:
            mtype_name = comp_m.group(1).strip('"')
            inst_name = comp_m.group(2)
            new_stmt_lines = list(stmt_lines)
            if mtype_name in {"Var", "VAR"} and var_updates:
                for vname, new_val in var_updates.items():
                    for li, ln in enumerate(new_stmt_lines):
                        upd, ok = _replace_param_in_code(ln, vname, new_val)
                        if ok:
                            new_stmt_lines[li] = upd
                            applied.add(f"VAR:{vname}")
                            break
            for (target_inst, target_param), new_val in inst_updates.items():
                if target_inst == inst_name:
                    for li, ln in enumerate(new_stmt_lines):
                        upd, ok = _replace_param_in_code(ln, target_param, new_val)
                        if ok:
                            new_stmt_lines[li] = upd
                            applied.add(f"{target_inst}.{target_param}")
                            break
            out_lines.extend(new_stmt_lines)
        else:
            out_lines.extend(stmt_lines)

    missing_refs: list[str] = []
    for vname in var_updates:
        if f"VAR:{vname}" not in applied:
            missing_refs.append(f"VAR:{vname}")
    for (iname, pname) in inst_updates:
        if f"{iname}.{pname}" not in applied:
            missing_refs.append(f"{iname}.{pname}")
    if missing_refs:
        raise NetlistError(f"Could not locate parameter(s) to update in netlist: {missing_refs}")

    trailing_nl = "\n" if text.endswith("\n") else ""
    result_text = "\n".join(out_lines) + trailing_nl
    # Re-parse verification to ensure every patched parameter took effect in executable AST
    reparsed = inspect_netlist(result_text)
    inst_by_name = {i["name"]: i for i in reparsed["instances"]}
    for vname, expected_val in var_updates.items():
        actual = reparsed["variables"].get(vname, {}).get("raw", "").strip()
        if actual != expected_val.strip():
            raise NetlistError(f"Parameter verification failed for VAR:{vname}: expected {expected_val!r}, parsed {actual!r}.")
    for (iname, pname), expected_val in inst_updates.items():
        actual = inst_by_name.get(iname, {}).get("parameters", {}).get(pname, "").strip()
        if actual != expected_val.strip():
            raise NetlistError(f"Parameter verification failed for {iname}.{pname}: expected {expected_val!r}, parsed {actual!r}.")
    return result_text


def inject_sweep_plan(
    text: str,
    *,
    var: str,
    start: float | str,
    stop: float | str,
    step: float | str | None = None,
    dec: int | None = None,
    lin: int | None = None,
    analysis: str | None = None,
    plan_name: str = "SW_BRIDGE",
) -> str:
    """Inject a native ADS parameter or frequency sweep safely across multi-line `\\`-continued statements.

    - Sweeping a circuit variable (e.g. `R_val`) over AC/S_Param/HB injects a native `ParamSweep`
      controller (`SimInstanceName[1]="<analysis>"`) + `SweepPlan`, preserving the inner frequency sweep.
    - Sweeping `freq` (or sweeping any variable on a `DC` analysis) cleanly rewrites the complete
      logical analysis statement (joining continuations and stripping stale `SweepVar`/`SweepPlan`).
    """
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", var):
        raise NetlistError(f"Invalid sweep variable name: {var!r}")
    parsed = inspect_netlist(text)
    solver_analyses = [a for a in parsed["analyses"] if a["type"] in SOLVER_ANALYSIS_TYPES]
    if not solver_analyses:
        raise NetlistError("Cannot inject sweep: netlist has no solver analysis controller (AC, S_Param, HB, DC).")
    target = next((a for a in solver_analyses if analysis is None or a["name"] == analysis), None)
    if target is None:
        raise NetlistError(f"Target analysis '{analysis}' not found. Available: {[a['name'] for a in solver_analyses]}")

    step_parts = [f"Start={start}", f"Stop={stop}"]
    if step is not None:
        step_parts.append(f"Step={step}")
    elif lin is not None:
        step_parts.append(f"Lin={int(lin)}")
    elif dec is not None:
        step_parts.append(f"Dec={int(dec)}")
    else:
        raise NetlistError("Provide one of 'step', 'lin', or 'dec' for sweep plan.")

    lines = text.lstrip("\ufeff").splitlines()
    ctrl_name = f"{plan_name}_CTRL"
    ctrl_re = re.compile(rf"^\s*ParamSweep\s*:\s*{re.escape(ctrl_name)}\b", re.IGNORECASE)
    plan_re = re.compile(rf"^\s*SweepPlan\s*:\s*{re.escape(plan_name)}\b", re.IGNORECASE)

    # Remove any previous bridge sweep controller / plan with the same names so re-injection updates cleanly
    filtered_groups: list[list[str]] = []
    for stmt_lines in _group_statement_lines(lines):
        joined = " ".join(
            _split_code_and_comment(ln)[0].rstrip().rstrip("\\").strip()
            for ln in stmt_lines
        ).strip()
        if ctrl_re.match(joined) or plan_re.match(joined):
            continue
        filtered_groups.append(stmt_lines)

    out_lines: list[str] = []
    use_param_sweep = var.lower() != "freq" and target["type"] in {"AC", "S_Param", "SP", "HarmonicBalance", "HB", "Tran"}

    if use_param_sweep:
        for grp in filtered_groups:
            out_lines.extend(grp)
        if var not in parsed["variables"]:
            out_lines.append(f"{var}={start}")
        out_lines.append(
            f'ParamSweep:{ctrl_name} SimInstanceName[1]="{target["name"]}" '
            f'SweepVar="{var}" SweepPlan="{plan_name}"'
        )
        out_lines.append(f"SweepPlan: {plan_name} " + " ".join(step_parts))
        return "\n".join(out_lines) + "\n"

    # Direct SweepVar/SweepPlan attachment on complete logical statement (handles trailing `\` continuations)
    patched = False
    target_prefix = f"{target['type']}:{target['name']}"
    for stmt_lines in filtered_groups:
        joined_code = " ".join(
            _split_code_and_comment(ln)[0].rstrip().rstrip("\\").strip()
            for ln in stmt_lines
        ).strip()
        if not patched and (joined_code.startswith(target_prefix + " ") or joined_code == target_prefix):
            cleaned_stmt = re.sub(r'\s+SweepVar\s*=\s*"[^"]*"', "", joined_code)
            cleaned_stmt = re.sub(r'\s+SweepPlan\s*=\s*"[^"]*"', "", cleaned_stmt)
            cleaned_stmt = re.sub(r"\s+(?:Start|Stop|Step|Lin|Dec)\s*=\s*[^\s]+(?:\s+(?:Hz|kHz|MHz|GHz))?", "", cleaned_stmt)
            out_lines.append(f'{cleaned_stmt} SweepVar="{var}" SweepPlan="{plan_name}"')
            patched = True
        else:
            out_lines.extend(stmt_lines)

    if var.lower() != "freq" and var not in parsed["variables"]:
        out_lines.append(f"{var}={start}")
    out_lines.append(f"SweepPlan: {plan_name} " + " ".join(step_parts))
    return "\n".join(out_lines) + "\n"


def strip_analysis_controllers(text: str, *, keep: set[str] | None = None) -> str:
    """Remove multi-line `\\`-continued ADS analysis/output controllers and companion extractors (e.g. ATAHB)."""
    keep_set = {k.strip() for k in (keep or set())}
    drop_types = SOLVER_ANALYSIS_TYPES | {"OutputPlan", "SweepPlan", "ParamSweep"}
    groups = _group_statement_lines(text.lstrip("\ufeff").splitlines())

    # Pass 1: identify stripped analysis/controller names so orphan ModelExtractor/ATAHB components are also dropped
    stripped_names: set[str] = set()
    kept_groups: list[tuple[list[str], str]] = []
    for stmt_lines in groups:
        joined = " ".join(
            _split_code_and_comment(ln)[0].rstrip().rstrip("\\").strip()
            for ln in stmt_lines
        ).strip()
        m = re.match(r'^\s*("[^"]+"|[A-Za-z_][A-Za-z0-9_]*)\s*:\s*([A-Za-z_][A-Za-z0-9_]*)\b', joined)
        if m and m.group(1).strip('"') in drop_types and m.group(2) not in keep_set:
            stripped_names.add(m.group(2))
            continue
        kept_groups.append((stmt_lines, joined))

    # Pass 2: drop companion Component statements (e.g. Component:tahb_HB1 Module="ATAHB") tied to stripped analyses
    out_lines: list[str] = []
    for stmt_lines, joined in kept_groups:
        m = re.match(r'^\s*Component\s*:\s*([A-Za-z_][A-Za-z0-9_]*)\b', joined)
        if m and m.group(1) not in keep_set:
            if re.search(r'\bModule\s*=\s*"ATAHB"', joined, re.IGNORECASE) or any(
                re.search(rf'\b(?:HB_Analysis|Tran_Analysis|SimInstanceName(?:\[\d+\])?)\s*=\s*"{re.escape(nm)}"', joined)
                for nm in stripped_names
            ):
                continue
        out_lines.extend(stmt_lines)
    return "\n".join(out_lines) + ("\n" if text.endswith("\n") else "")
