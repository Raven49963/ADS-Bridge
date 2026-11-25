"""Offline Keysight ADS documentation search, built-in/custom PDK discovery, and doctor checks."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import struct
from typing import Any


DEFAULT_ADS_ROOT = Path(r"C:\Program Files\Keysight\ADS2026_Update0.1")

_DOC_INDEX_CACHE: dict[str, list[dict[str, Any]]] = {}
_TAG_RE = re.compile(r"<[^>]+>")
_OA_UPPER_RE = re.compile(r"%([A-Z])")
_OA_HEX_RE = re.compile(r"#([0-9a-fA-F]{2})")


def decode_oa_name(fs_name: str) -> str:
    """Decode OpenAccess Windows NTFS case-preserving '%X' uppercase and '#xx' hex escapes."""
    decoded = _OA_UPPER_RE.sub(r"\1", fs_name)
    decoded = _OA_HEX_RE.sub(lambda m: chr(int(m.group(1), 16)), decoded)
    return decoded


def _read_html_snippet(html_path: Path, query_low: str, terms: list[str]) -> str:
    if not html_path.is_file():
        return ""
    try:
        raw = html_path.read_text(encoding="utf-8", errors="replace")[:48000]
    except OSError:
        return ""
    # Strip script/style blocks quickly then tags
    cleaned = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw, flags=re.IGNORECASE | re.DOTALL)
    plain = " ".join(_TAG_RE.sub(" ", cleaned).split())
    low = plain.casefold()
    pos = low.find(query_low)
    if pos < 0:
        pos = next((low.find(t) for t in terms if low.find(t) >= 0), 0)
    start = max(0, pos - 60)
    return plain[start:start + 260].strip()


def resolve_ads_root(configured: str | Path | None = None) -> Path:
    if configured:
        return Path(configured).resolve()
    env_dir = os.environ.get("HPEESOF_DIR")
    if env_dir:
        return Path(env_dir).resolve()
    if DEFAULT_ADS_ROOT.exists():
        return DEFAULT_ADS_ROOT.resolve()
    keysight_dir = Path(r"C:\Program Files\Keysight")
    if keysight_dir.is_dir():
        candidates = sorted(keysight_dir.glob("ADS*"), reverse=True)
        if candidates:
            return candidates[0].resolve()
    return DEFAULT_ADS_ROOT


def configured_pdk_roots(extra_roots: list[str | Path] | None = None) -> list[Path]:
    roots: list[Path] = []
    raw_env = os.environ.get("ADS_BRIDGE_PDK_ROOTS", "")
    for chunk in raw_env.split(os.pathsep):
        if chunk.strip():
            roots.append(Path(chunk.strip()).expanduser())
    for item in extra_roots or ():
        if item:
            roots.append(Path(item).expanduser())
    unique: list[Path] = []
    for r in roots:
        try:
            rp = r.resolve()
        except OSError:
            continue
        if rp.is_dir() and rp not in unique:
            unique.append(rp)
    return unique


def _build_doc_index(ads_root: Path) -> list[dict[str, Any]]:
    cache_key = str(ads_root)
    if cache_key in _DOC_INDEX_CACHE:
        return _DOC_INDEX_CACHE[cache_key]

    entries: list[dict[str, Any]] = []
    doc_root = ads_root / "doc"
    if not doc_root.is_dir():
        _DOC_INDEX_CACHE[cache_key] = entries
        return entries

    py_root = doc_root / "python"
    if py_root.is_dir():
        for pkg_dir in sorted(py_root.iterdir()):
            if not pkg_dir.is_dir():
                continue
            # 1. Python/AEL examples under doc/python/<pkg>/examples
            ex_dir = pkg_dir / "examples"
            if ex_dir.is_dir():
                for path in sorted(ex_dir.rglob("*")):
                    if path.is_file() and path.suffix.lower() in {".py", ".ael"}:
                        try:
                            raw = path.read_text(encoding="utf-8", errors="replace")[:32000]
                        except OSError:
                            continue
                        first_line = next((ln.strip(" #\"'") for ln in raw.splitlines() if ln.strip()), path.name)
                        entries.append({
                            "title": f"{pkg_dir.name}: {path.stem}",
                            "category": "example",
                            "path": str(path),
                            "rel_path": str(path.relative_to(ads_root)),
                            "summary": first_line[:220],
                            "search_blob": f"{pkg_dir.name} {path.stem} {raw}".casefold(),
                            "raw_text": raw,
                        })

            # 2. Pre-built Sphinx searchindex.js under doc/python/<pkg>/html/searchindex.js
            sidx = pkg_dir / "html" / "searchindex.js"
            if sidx.is_file():
                try:
                    js_text = sidx.read_text(encoding="utf-8", errors="replace").strip()
                    prefix = "Search.setIndex("
                    if js_text.startswith(prefix) and js_text.endswith(")"):
                        data = json.loads(js_text[len(prefix):-1])
                    else:
                        continue
                except Exception:
                    continue
                docnames: list[str] = data.get("docnames") or []
                titles: list[str] = data.get("titles") or []
                # Collect section titles & API object names per doc index
                doc_keywords: list[list[str]] = [[] for _ in range(len(docnames))]
                for sec_title, refs in (data.get("alltitles") or {}).items():
                    for ref in refs:
                        if isinstance(ref, list) and ref and isinstance(ref[0], int) and 0 <= ref[0] < len(docnames):
                            doc_keywords[ref[0]].append(sec_title)
                for obj_prefix, members in (data.get("objects") or {}).items():
                    if isinstance(members, dict):
                        for member_name, meta in members.items():
                            doc_idx = meta[0] if isinstance(meta, list) and meta and isinstance(meta[0], int) else -1
                            if 0 <= doc_idx < len(docnames):
                                doc_keywords[doc_idx].append(f"{obj_prefix}.{member_name}" if obj_prefix else member_name)

                for idx, docname in enumerate(docnames):
                    title = titles[idx] if idx < len(titles) else docname
                    html_path = pkg_dir / "html" / f"{docname}.html"
                    kw_str = " ".join(doc_keywords[idx][:120])
                    entries.append({
                        "title": f"[{pkg_dir.name}] {title}",
                        "category": "python_api",
                        "path": str(html_path),
                        "rel_path": str(html_path.relative_to(ads_root)) if html_path.is_relative_to(ads_root) else str(html_path),
                        "summary": kw_str[:240] or title,
                        "search_blob": f"{pkg_dir.name} {docname} {title} {kw_str}".casefold(),
                        "raw_text": None,
                    })

    err_html = doc_root / "error_warning_info.html"
    if err_html.is_file():
        entries.append({
            "title": "ADS Error and Warning Reference",
            "category": "diagnostics",
            "path": str(err_html),
            "rel_path": str(err_html.relative_to(ads_root)),
            "summary": "Keysight ADS simulator and design environment error/warning reference.",
            "search_blob": "ads error warning hpeesofsim diagnostic reference",
            "raw_text": None,
        })

    _DOC_INDEX_CACHE[cache_key] = entries
    return entries


def search_docs(
    query: str,
    *,
    category: str = "all",
    limit: int = 10,
    ads_root: str | Path | None = None,
) -> dict[str, Any]:
    """Search installed Keysight ADS Python API docs, examples, and diagnostic guides in milliseconds."""
    if not isinstance(query, str) or len(query.strip()) < 2 or len(query) > 200:
        raise ValueError("Query must be between 2 and 200 characters.")
    if category not in {"all", "example", "python_api", "diagnostics"}:
        raise ValueError("category must be 'all', 'example', 'python_api', or 'diagnostics'.")
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError("limit must be an integer between 1 and 50.")

    root = resolve_ads_root(ads_root)
    index = _build_doc_index(root)
    q_clean = query.strip().casefold()
    terms = [t for t in re.split(r"\s+", q_clean) if t]

    scored: list[tuple[float, dict[str, Any]]] = []
    for item in index:
        if category != "all" and item["category"] != category:
            continue
        title_low = item["title"].casefold()
        rel_low = item["rel_path"].casefold()
        blob = item["search_blob"]

        score = 0.0
        if q_clean in title_low:
            score += 12.0
        if q_clean in rel_low:
            score += 10.0
        if q_clean in blob:
            score += 6.0

        matched_terms = 0
        for term in terms:
            hit = False
            if term in title_low:
                score += 4.0
                hit = True
            if term in rel_low:
                score += 3.0
                hit = True
            count = blob.count(term)
            if count:
                score += min(count, 8) * 0.75
                hit = True
            if hit:
                matched_terms += 1

        if matched_terms == 0:
            continue
        if matched_terms == len(terms):
            score += 5.0
        scored.append((score, item))

    scored.sort(key=lambda pair: (-pair[0], pair[1]["rel_path"]))
    results: list[dict[str, Any]] = []
    for score, item in scored[:limit]:
        if item["raw_text"] is not None:
            raw_low = item["raw_text"].casefold()
            pos = raw_low.find(q_clean)
            if pos < 0:
                pos = next((raw_low.find(t) for t in terms if raw_low.find(t) >= 0), 0)
            start = max(0, pos - 60)
            snippet = item["raw_text"][start:start + 260].replace("\n", " ").strip()
        else:
            snippet = _read_html_snippet(Path(item["path"]), q_clean, terms) or item["summary"]
        results.append({
            "title": item["title"],
            "category": item["category"],
            "path": item["path"],
            "rel_path": item["rel_path"],
            "score": round(score, 2),
            "snippet": snippet,
        })

    return {
        "query": query,
        "ads_root": str(root),
        "indexed_documents": len(index),
        "total_matches": len(scored),
        "results": results,
    }


def _parse_lib_defs_file(defs_file: Path, base_vars: dict[str, str], visited: set[Path] | None = None) -> list[dict[str, Any]]:
    if visited is None:
        visited = set()
    real = defs_file.resolve()
    if real in visited or not real.is_file():
        return []
    visited.add(real)

    results: list[dict[str, Any]] = []
    try:
        lines = real.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return results

    def expand(raw_path: str) -> Path:
        expanded = raw_path.strip().strip('"')
        for k, v in base_vars.items():
            expanded = expanded.replace(f"${k}", v).replace(f"${{{k}}}", v)
        p = Path(expanded)
        if not p.is_absolute():
            p = (real.parent / p).resolve()
        return p

    for raw in lines:
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        cmd = parts[0].upper()
        if cmd == "DEFINE" and len(parts) >= 3:
            lib_name = parts[1]
            lib_path = expand(" ".join(parts[2:]))
            results.append({
                "name": lib_name,
                "path": str(lib_path),
                "defs_file": str(real),
                "exists": lib_path.is_dir(),
            })
        elif cmd == "INCLUDE" and len(parts) >= 2:
            inc_path = expand(" ".join(parts[1:]))
            results.extend(_parse_lib_defs_file(inc_path, base_vars, visited))
    return results


def discover_filesystem_libraries(
    *,
    ads_root: str | Path | None = None,
    pdk_roots: list[str | Path] | None = None,
    include_builtin: bool = True,
) -> list[dict[str, Any]]:
    root = resolve_ads_root(ads_root)
    base_vars = {"HPEESOF_DIR": str(root)}
    seen: dict[str, dict[str, Any]] = {}

    if include_builtin:
        oalibs = root / "oalibs"
        if oalibs.is_dir():
            for defs in sorted(oalibs.glob("*.defs")):
                for entry in _parse_lib_defs_file(defs, base_vars):
                    if entry["name"] not in seen:
                        seen[entry["name"]] = {**entry, "source": "ads_builtin"}

    for pdk_root in configured_pdk_roots(pdk_roots):
        defs_candidates = list(pdk_root.glob("**/lib.defs"))[:20]
        for defs in defs_candidates:
            for entry in _parse_lib_defs_file(defs, base_vars):
                seen[entry["name"]] = {**entry, "source": "pdk"}
        # If the PDK directory itself is an OpenAccess library with cdsinfo.tag
        if (pdk_root / "cdsinfo.tag").is_file() and pdk_root.name not in seen:
            seen[pdk_root.name] = {
                "name": pdk_root.name,
                "path": str(pdk_root),
                "defs_file": None,
                "exists": True,
                "source": "pdk",
            }

    return sorted(seen.values(), key=lambda x: (x["source"] != "pdk", x["name"].lower()))


def list_pdk(
    *,
    ads_root: str | Path | None = None,
    pdk_roots: list[str | Path] | None = None,
    include_builtin: bool = True,
) -> dict[str, Any]:
    libs = discover_filesystem_libraries(ads_root=ads_root, pdk_roots=pdk_roots, include_builtin=include_builtin)
    return {
        "total": len(libs),
        "pdk_roots": [str(p) for p in configured_pdk_roots(pdk_roots)],
        "libraries": libs,
    }


def describe_pdk(
    library_name: str,
    *,
    ads_root: str | Path | None = None,
    pdk_roots: list[str | Path] | None = None,
) -> dict[str, Any]:
    if not isinstance(library_name, str) or not library_name.strip():
        raise ValueError("library_name must be a non-empty string.")
    libs = discover_filesystem_libraries(ads_root=ads_root, pdk_roots=pdk_roots, include_builtin=True)
    match = next((item for item in libs if item["name"] == library_name), None)
    if match is None:
        raise KeyError(f"Library '{library_name}' was not found in ADS builtin oalibs or configured PDK roots.")

    lib_path = Path(match["path"])
    cells: list[dict[str, Any]] = []
    view_counts: dict[str, int] = {}
    if lib_path.is_dir():
        for child in sorted(lib_path.iterdir()):
            if not child.is_dir() or child.name.startswith("."):
                continue
            cell_name = decode_oa_name(child.name)
            views = sorted(
                decode_oa_name(v.name) for v in child.iterdir()
                if v.is_dir() and ((v / "master.tag").is_file() or any(v.iterdir()))
            )
            if views:
                cells.append({"cell": cell_name, "views": views})
                for vname in views:
                    view_counts[vname] = view_counts.get(vname, 0) + 1

    # Search nearby model and tech files
    parent_root = lib_path.parent
    tech_files = [str(lib_path / fn) for fn in ("tech.db", "tech.subst", "materials.matdb", "display.tech") if (lib_path / fn).is_file()]
    model_files: list[str] = []
    for search_dir in (lib_path, parent_root / "circuit" / "models", parent_root / "models"):
        if search_dir.is_dir():
            for ext in ("*.model", "*.ckt", "*.net", "*.scs", "ADSlibconfig"):
                for mf in sorted(search_dir.glob(ext))[:25]:
                    if str(mf) not in model_files:
                        model_files.append(str(mf))

    return {
        "library": match["name"],
        "source": match["source"],
        "path": match["path"],
        "exists": match["exists"],
        "cell_count": len(cells),
        "view_counts": view_counts,
        "tech_files": tech_files,
        "model_files": model_files[:30],
        "sample_cells": cells[:50],
    }


_PDK_CELL_INDEX_CACHE: dict[tuple[str, tuple[str, ...]], list[dict[str, Any]]] = {}


def _get_pdk_cell_index(
    ads_root: str | Path | None = None,
    pdk_roots: list[str | Path] | None = None,
) -> list[dict[str, Any]]:
    root = resolve_ads_root(ads_root)
    pdk_key = tuple(str(p) for p in configured_pdk_roots(pdk_roots))
    cache_key = (str(root), pdk_key)
    cached = _PDK_CELL_INDEX_CACHE.get(cache_key)
    if cached is not None:
        return cached
    libs = discover_filesystem_libraries(ads_root=root, pdk_roots=pdk_roots, include_builtin=True)
    entries: list[dict[str, Any]] = []
    for lib in libs:
        lib_path = Path(lib["path"])
        if not lib_path.is_dir():
            continue
        lname = lib["name"]
        lname_low = lname.casefold()
        source = lib["source"]
        try:
            children = list(lib_path.iterdir())
        except OSError:
            continue
        for child in children:
            if not child.is_dir() or child.name.startswith("."):
                continue
            cell_name = decode_oa_name(child.name)
            try:
                views = sorted(decode_oa_name(v.name) for v in child.iterdir() if v.is_dir())
            except OSError:
                continue
            if not views:
                continue
            entries.append({
                "library": lname,
                "library_low": lname_low,
                "cell": cell_name,
                "cell_low": cell_name.casefold(),
                "master": f"{lname}:{cell_name}",
                "source": source,
                "views": views,
                "symbol": f"{lname}:{cell_name}:symbol" if "symbol" in views else None,
            })
    _PDK_CELL_INDEX_CACHE[cache_key] = entries
    return entries


def search_pdk(
    query: str,
    *,
    library_name: str | None = None,
    ads_root: str | Path | None = None,
    pdk_roots: list[str | Path] | None = None,
    limit: int = 25,
) -> dict[str, Any]:
    if not isinstance(query, str) or len(query.strip()) < 1 or len(query) > 128:
        raise ValueError("query must be between 1 and 128 characters.")
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100.")

    index = _get_pdk_cell_index(ads_root=ads_root, pdk_roots=pdk_roots)
    q_low = query.strip().casefold()
    scored_hits: list[tuple[int, str, str, dict[str, Any]]] = []
    for item in index:
        if library_name and item["library"] != library_name:
            continue
        cname_low = item["cell_low"]
        lname_low = item["library_low"]
        if cname_low == q_low:
            rank = 0
        elif cname_low.startswith(q_low):
            rank = 1
        elif q_low in cname_low:
            rank = 2
        elif q_low in lname_low:
            rank = 3
        else:
            continue
        scored_hits.append((
            rank,
            cname_low,
            lname_low,
            {
                "library": item["library"],
                "cell": item["cell"],
                "master": item["master"],
                "source": item["source"],
                "views": item["views"],
                "symbol": item["symbol"],
            },
        ))
    scored_hits.sort(key=lambda item: (item[0], len(item[1]), item[1], item[2]))
    matches = [item[3] for item in scored_hits[:limit]]
    return {
        "query": query,
        "total_matches": len(scored_hits),
        "returned": len(matches),
        "limit_reached": len(scored_hits) > limit,
        "matches": matches,
    }


def _pe_machine_bits(exe_path: Path) -> int | None:
    """Read PE COFF header without spawning a process (avoids Windows missing-DLL modal popups)."""
    try:
        with exe_path.open("rb") as f:
            if f.read(2) != b"MZ":
                return None
            f.seek(0x3C)
            pe_offset = struct.unpack("<I", f.read(4))[0]
            f.seek(pe_offset)
            if f.read(4) != b"PE\x00\x00":
                return None
            machine = struct.unpack("<H", f.read(2))[0]
            return {0x8664: 64, 0xAA64: 64, 0x014C: 32}.get(machine)
    except OSError:
        return None


def environment_status(runtime: Path, sessions: list[dict[str, Any]], *, ads_root: str | Path | None = None) -> dict[str, Any]:
    """Comprehensive local installation, simulator, session, and runtime health report."""
    root = resolve_ads_root(ads_root)
    ads_python = root / "tools" / "python" / "python.exe"
    simulator_exe = root / "bin" / "hpeesofsim.exe"
    sim_bits = _pe_machine_bits(simulator_exe) if simulator_exe.is_file() else None
    py_bits = _pe_machine_bits(ads_python) if ads_python.is_file() else None

    reachable = [s for s in sessions if s.get("reachable")]
    return {
        "ok": root.is_dir() and ads_python.is_file() and simulator_exe.is_file() and sim_bits == 64,
        "ads_root": str(root),
        "ads_version": root.name,
        "ads_python": str(ads_python),
        "ads_python_exists": ads_python.is_file(),
        "ads_python_bits": py_bits,
        "simulator_exe": str(simulator_exe),
        "simulator_exists": simulator_exe.is_file(),
        "simulator_bits": sim_bits,
        "simulator_banner": f"hpeesofsim ({sim_bits}-bit Windows) @ {root.name}" if sim_bits else None,
        "runtime_dir": str(runtime),
        "runtime_exists": runtime.exists(),
        "registered_sessions": len(sessions),
        "reachable_sessions": len(reachable),
        "sessions": sessions,
        "pdk_roots": [str(p) for p in configured_pdk_roots()],
    }
