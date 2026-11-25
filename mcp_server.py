"""MCP stdio adapter for ADS Bridge 2.0.

Strict stdio hygiene: all logging and warnings go exclusively to stderr;
stdout carries only newline-delimited JSON-RPC 2.0 MCP frames.
"""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path
import sys
from typing import Any
import warnings

# Enforce stderr-only logging and warning output before importing submodules
logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
warnings.showwarning = lambda msg, cat, fn, ln, file=None, line=None: sys.stderr.write(
    warnings.formatwarning(msg, cat, fn, ln, line)
)

from mcp.server.fastmcp import FastMCP, Image  # noqa: E402

from bridge import (  # noqa: E402
    ParameterValue,
    STANDALONE_SESSION,
    bridge_status as get_status,
    call,
    dataset_analyze as analyze_trace,
    dataset_catalog,
    dataset_compress as compress_ds,
    dataset_read,
    dataset_table as read_table,
    dds_build as build_dds_file,
    design_eval_step as run_eval_step,
    docs_search as query_docs,
    gui_launch as launch_ads_gui,
    headless_call as run_headless,
    job_cancel,
    job_get,
    job_list,
    job_log,
    job_log_summary as read_log_summary,
    job_result,
    list_sessions,
    netlist_check as check_nl,
    netlist_compress as compress_nl,
    netlist_inspect as inspect_nl,
    netlist_patch as patch_nl,
    netlist_register as register_nl,
    optimize_netlist as run_opt_netlist,
    pdk_describe as inspect_pdk_lib,
    pdk_list as list_pdk_libs,
    pdk_search as search_pdk_cells,
    request_get as read_request,
    sensitivity_scan as run_sensitivity,
    simulate_batch as run_sim_batch,
    simulate_netlist as run_sim_netlist,
    touchstone_compress as compress_ts,
)


server = FastMCP(
    "ADS Bridge",
    instructions=(
        "Keysight ADS 2026 Update 0.1 bridge supporting both live ADS GUI sessions and standalone "
        "64-bit headless automation. Use bridge_status or session_list first. Before placing or wiring "
        "unknown components, call component_describe to inspect exact pin names/numbers, snap offsets, "
        "and parameter shapes. Supply every row and field for repeated or compound parameters. "
        "Wire schematics by terminal endpoints ('R1.2' -> 'C1.1') or coordinates, check "
        "connectivity via design_check / netlist_check, run 64-bit simulations, and verify results via "
        "dataset_describe, dataset_trace, and dataset_analyze."
    ),
)


# ---------------------------------------------------------------------------
# Environment, Sessions, Docs & PDK Discovery
# ---------------------------------------------------------------------------


@server.tool()
def bridge_status() -> dict[str, Any]:
    """Check ADS 2026 installation, 64-bit simulator, runtime health, PDK roots, and live sessions."""
    return get_status()


@server.tool()
def session_list() -> list[dict[str, Any]]:
    """List registered local ADS GUI sessions and whether each endpoint responds."""
    return list_sessions()


@server.tool()
def session_inspect(session_id: str, challenge: str = "") -> dict[str, Any]:
    """Read the active ADS GUI workspace and echo a challenge from the ADS GUI main thread."""
    return call(session_id, "session.inspect", challenge=challenge)


@server.tool()
def workspace_open(session_id: str, path: str) -> dict[str, Any]:
    """Open an existing workspace under a locally configured root when ADS GUI has no workspace open."""
    return call(session_id, "workspace.open", path=path)


@server.tool(structured_output=False)
def session_capture(session_id: str) -> tuple[Image, str]:
    """Capture the ADS main window as a PNG and return its inline image and local path."""
    path = Path(call(session_id, "session.capture")["artifact"])
    return Image(path=path), str(path)


@server.tool()
def docs_search(query: str, category: str = "all", limit: int = 10) -> dict[str, Any]:
    """Search installed Keysight ADS Python API documentation, examples, and diagnostic guides."""
    return query_docs(query, category=category, limit=limit)


@server.tool()
def pdk_list(include_builtin: bool = True) -> dict[str, Any]:
    """List ADS built-in component libraries (oalibs) and locally configured PDK libraries."""
    return list_pdk_libs(include_builtin=include_builtin)


@server.tool()
def pdk_describe(library: str) -> dict[str, Any]:
    """Inspect a filesystem ADS or PDK library: cell/view counts, tech files, models, and sample cells."""
    return inspect_pdk_lib(library)


@server.tool()
def pdk_search(query: str, library: str | None = None, limit: int = 25) -> dict[str, Any]:
    """Find cells/components by name across installed ADS libraries and configured PDK roots."""
    return search_pdk_cells(query, library=library, limit=limit)


# ---------------------------------------------------------------------------
# Catalog, Component Introspection, Schematics & Incremental Editing
# ---------------------------------------------------------------------------


@server.tool()
def catalog_list(
    session_id: str,
    workspace_id: str,
    library: str | None = None,
    search: str = "",
    offset: int = 0,
    limit: int = 100,
) -> dict[str, Any]:
    """Search workspace libraries, or list and search cellviews in a specified library."""
    return call(
        session_id,
        "catalog.list",
        workspace_id=workspace_id,
        library=library,
        search=search,
        offset=offset,
        limit=limit,
    )


@server.tool()
def component_describe(
    session_id: str,
    workspace_id: str,
    master: str,
    compact: bool = False,
) -> dict[str, Any]:
    """Inspect a component symbol's exact pin numbers/names, polarity hints, snap offsets, and parameter definitions."""
    kwargs: dict[str, Any] = {"workspace_id": workspace_id, "master": master}
    if compact:
        kwargs["compact"] = True
    return call(session_id, "component.describe", **kwargs)


@server.tool()
def library_create(
    session_id: str,
    workspace_id: str,
    name: str,
    request_id: str,
    layout_technology: bool = False,
) -> dict[str, Any]:
    """Create a new local library. Enable layout_technology to initialize ADS standard mm layout tech."""
    return call(
        session_id,
        "library.create",
        workspace_id=workspace_id,
        name=name,
        request_id=request_id,
        layout_technology=layout_technology,
    )


@server.tool()
def design_list(session_id: str, workspace_id: str) -> dict[str, Any]:
    """List cellviews in writable libraries of the active workspace."""
    return call(session_id, "design.list", workspace_id=workspace_id)


@server.tool()
def design_read(session_id: str, workspace_id: str, design: str) -> dict[str, Any]:
    """Read a schematic, its instances, pin coordinates, net connections, ports, AI summary, and revision."""
    return call(session_id, "design.read", workspace_id=workspace_id, design=design)


@server.tool()
def design_check(session_id: str, workspace_id: str, design: str, mode: str = "tb") -> dict[str, Any]:
    """Run OpenAccess pin/net connectivity and semantic ADS netlist ERC ('tb' or 'dut') on a schematic."""
    return call(session_id, "design.check", workspace_id=workspace_id, design=design, mode=mode)


@server.tool()
def design_build(
    session_id: str,
    workspace_id: str,
    design: str,
    instances: list[dict[str, Any]],
    wires: list[dict[str, Any]],
    request_id: str,
    pins: list[dict[str, Any]] | None = None,
    variables: dict[str, str] | None = None,
    replace: bool = False,
) -> dict[str, Any]:
    """Create a schematic from instances, wires, pins, and VARs. Instance parameters can be strings or full arrays."""
    return call(
        session_id,
        "design.build",
        workspace_id=workspace_id,
        design=design,
        instances=instances,
        wires=wires,
        pins=pins,
        variables=variables,
        replace=replace,
        request_id=request_id,
    )


@server.tool()
def design_edit(
    session_id: str,
    workspace_id: str,
    design: str,
    revision: str,
    changes: list[dict[str, Any]],
    request_id: str,
) -> dict[str, Any]:
    """Edit parameters transactionally. Use full arrays for repeated values and nested rows for repeated compounds."""
    return call(
        session_id,
        "design.edit",
        workspace_id=workspace_id,
        design=design,
        revision=revision,
        changes=changes,
        request_id=request_id,
    )


@server.tool()
def design_update(
    session_id: str,
    workspace_id: str,
    design: str,
    revision: str,
    ops: list[dict[str, Any]],
    request_id: str,
) -> dict[str, Any]:
    """Apply atomic incremental schematic ops (add_instance, add_wire, add_pin, set_var, set_param, delete_instance)."""
    return call(
        session_id,
        "design.update",
        workspace_id=workspace_id,
        design=design,
        revision=revision,
        ops=ops,
        request_id=request_id,
    )


@server.tool()
def design_add_instance(
    session_id: str,
    workspace_id: str,
    design: str,
    revision: str,
    name: str,
    master: str,
    at: list[float],
    request_id: str,
    angle: float = 0.0,
    parameters: dict[str, ParameterValue] | None = None,
) -> dict[str, Any]:
    """Place a component transactionally. Parameter arrays must match the component's existing ADS shape."""
    return call(
        session_id,
        "design.add_instance",
        workspace_id=workspace_id,
        design=design,
        revision=revision,
        name=name,
        master=master,
        at=at,
        angle=angle,
        parameters=parameters,
        request_id=request_id,
    )


@server.tool()
def design_add_wire(
    session_id: str,
    workspace_id: str,
    design: str,
    revision: str,
    request_id: str,
    from_endpoint: str | None = None,
    to_endpoint: str | None = None,
    points: list[list[float]] | None = None,
    label: str | None = None,
) -> dict[str, Any]:
    """Wire two terminals ('R1.2' -> 'C1.1' or 'PIN:IN') with orthogonal routing, or wire explicit points."""
    return call(
        session_id,
        "design.add_wire",
        workspace_id=workspace_id,
        design=design,
        revision=revision,
        from_endpoint=from_endpoint,
        to_endpoint=to_endpoint,
        points=points,
        label=label,
        request_id=request_id,
    )


@server.tool()
def design_add_pin(
    session_id: str,
    workspace_id: str,
    design: str,
    revision: str,
    name: str,
    request_id: str,
    direction: str = "inputOutput",
    at: list[float] | None = None,
    connect: str | None = None,
    angle: float = 0.0,
) -> dict[str, Any]:
    """Create a schematic port pin and optionally auto-wire it to an instance terminal ('R1.1')."""
    return call(
        session_id,
        "design.add_pin",
        workspace_id=workspace_id,
        design=design,
        revision=revision,
        name=name,
        direction=direction,
        at=at,
        connect=connect,
        angle=angle,
        request_id=request_id,
    )


@server.tool()
def design_set_parameter(
    session_id: str,
    workspace_id: str,
    design: str,
    revision: str,
    instance: str,
    parameter: str,
    magnitude: float,
    unit: str,
    request_id: str,
) -> dict[str, Any]:
    """Set R, C, or L with explicit SI units in the writable test workspace. Provide a 32-hex request_id."""
    return call(
        session_id,
        "design.set_parameter",
        workspace_id=workspace_id,
        design=design,
        revision=revision,
        instance=instance,
        parameter=parameter,
        magnitude=magnitude,
        unit=unit,
        request_id=request_id,
    )


# ---------------------------------------------------------------------------
# Netlist Generation, AI Comprehension, Layout & GDSII
# ---------------------------------------------------------------------------


@server.tool()
def netlist_generate(session_id: str, workspace_id: str, design: str, inspect: bool = True) -> dict[str, Any]:
    """Generate ADS netlist text, SHA-256 revision, and structured AI-readable AST/topology from a schematic."""
    return call(session_id, "netlist.generate", workspace_id=workspace_id, design=design, inspect=inspect)


@server.tool()
def netlist_inspect(netlist_or_path: str, search_dirs: list[str] | None = None) -> dict[str, Any]:
    """Parse raw ADS netlist text or a .ckt file into structured instances, nets, analyses, and AI summary."""
    return inspect_nl(netlist_or_path, search_dirs=search_dirs)


@server.tool()
def netlist_check(netlist_or_path: str, mode: str = "tb", search_dirs: list[str] | None = None) -> dict[str, Any]:
    """Run semantic electrical-rule checks ('tb' or 'dut' mode) on raw ADS netlist text or a .ckt file."""
    return check_nl(netlist_or_path, mode=mode, search_dirs=search_dirs)


@server.tool()
def layout_build(
    session_id: str,
    workspace_id: str,
    design: str,
    shapes: list[dict[str, Any]],
    request_id: str,
    replace: bool = False,
) -> dict[str, Any]:
    """Create a layout from rectangles and polygons {type, layer, points}."""
    return call(
        session_id,
        "layout.build",
        workspace_id=workspace_id,
        design=design,
        shapes=shapes,
        replace=replace,
        request_id=request_id,
    )


@server.tool()
def layout_read(session_id: str, workspace_id: str, design: str, limit: int = 100) -> dict[str, Any]:
    """Read layout bounding box and shape geometry summaries."""
    return call(session_id, "layout.read", workspace_id=workspace_id, design=design, limit=limit)


@server.tool()
def layout_export_gds(session_id: str, workspace_id: str, design: str) -> dict[str, Any]:
    """Export a GDSII file into the bridge artifact directory using the library layout technology."""
    return call(session_id, "layout.export_gds", workspace_id=workspace_id, design=design)


# ---------------------------------------------------------------------------
# Simulations, Standalone/Headless Execution, Datasets & Request Ledger
# ---------------------------------------------------------------------------


@server.tool()
def simulation_submit(
    session_id: str,
    workspace_id: str,
    design: str,
    revision: str,
    request_id: str,
    block: str | None = None,
    axis: str | None = None,
    variable: str | None = None,
) -> dict[str, Any]:
    """Submit a circuit simulation job from a live ADS GUI schematic."""
    return call(
        session_id,
        "simulation.submit",
        workspace_id=workspace_id,
        design=design,
        revision=revision,
        block=block,
        axis=axis,
        variable=variable,
        request_id=request_id,
    )


@server.tool()
def simulate_netlist(
    netlist_or_path: str,
    request_id: str | None = None,
    block: str | None = None,
    axis: str | None = None,
    variable: str | None = None,
    data_dir: str | None = None,
    model_dirs: list[str] | None = None,
    sweep: dict[str, Any] | None = None,
    save_to_data_dir: bool = False,
    dataset_name: str | None = None,
    wait: bool = True,
    timeout: float = 180.0,
) -> dict[str, Any]:
    """Simulate an ADS netlist string or .ckt file directly in standalone 64-bit ADS without ADS GUI."""
    return run_sim_netlist(
        netlist_or_path,
        session_id=STANDALONE_SESSION,
        request_id=request_id,
        block=block,
        axis=axis,
        variable=variable,
        data_dir=data_dir,
        model_dirs=model_dirs,
        sweep=sweep,
        save_to_data_dir=save_to_data_dir,
        dataset_name=dataset_name,
        wait=wait,
        timeout=timeout,
    )


@server.tool()
def simulate_batch(
    netlists: list[str],
    max_workers: int = 2,
    timeout: float = 300.0,
    model_dirs: list[str] | None = None,
) -> dict[str, Any]:
    """Run a batch of ADS netlists or .ckt files in parallel via standalone 64-bit ADS."""
    return run_sim_batch(netlists, max_workers=max_workers, timeout=timeout, model_dirs=model_dirs)


@server.tool()
def simulation_status(session_id: str, job_id: str) -> dict[str, Any]:
    """Read a persisted simulation job status (pass session_id='standalone' for standalone jobs)."""
    return job_get(session_id, job_id)


@server.tool()
def simulation_cancel(session_id: str, job_id: str) -> dict[str, Any]:
    """Cancel a running simulation job and terminate its Windows hpeesofsim process tree."""
    return job_cancel(session_id, job_id)


@server.tool()
def simulation_result(session_id: str, job_id: str) -> dict[str, Any]:
    """Read the preview trace selected at submission time."""
    return job_result(session_id, job_id)


@server.tool()
def simulation_list(session_id: str, limit: int = 50) -> list[dict[str, Any]]:
    """List persisted simulation jobs for an ADS GUI session or 'standalone'."""
    return job_list(session_id, limit)


@server.tool()
def simulation_log(session_id: str, job_id: str, stream: str = "sim") -> dict[str, Any]:
    """Read the tail of the simulation ('sim') or worker ('worker') log."""
    return job_log(session_id, job_id, stream)


@server.tool()
def dataset_describe(session_id: str, job_id: str) -> dict[str, Any]:
    """List dataset blocks, independent sweep axes, and dependent variables."""
    return dataset_catalog(session_id, job_id)


@server.tool()
def dataset_trace(
    session_id: str,
    job_id: str,
    block: str,
    axis: str,
    variable: str,
    offset: int = 0,
    limit: int = 250,
    filters: dict[str, int | float | str] | None = None,
) -> dict[str, Any]:
    """Read a paginated complex trace from a completed simulation dataset."""
    return dataset_read(session_id, job_id, block, axis, variable, offset, limit, filters)


@server.tool()
def dataset_table(
    session_id: str,
    job_id: str,
    block: str,
    columns: list[str],
    offset: int = 0,
    limit: int = 100,
) -> dict[str, Any]:
    """Read multiple dataset columns containing scalars, text, or complex {real, imag} values."""
    return read_table(session_id, job_id, block, columns, offset, limit)


@server.tool()
def dataset_analyze(
    session_id: str,
    job_id: str,
    block: str,
    axis: str,
    signals: list[str] | None = None,
    at: list[float] | None = None,
    filters: dict[str, int | float | str] | None = None,
) -> dict[str, Any]:
    """Compute dB/phase/min/max/RMS stats, -3 dB cutoffs, 2-port S-param passivity/reciprocity, and 'at' samples."""
    return analyze_trace(session_id, job_id, block, axis, signals=signals, at=at, filters=filters)


@server.tool()
def netlist_compress(
    netlist_or_path: str,
    search_dirs: list[str] | None = None,
    include_models: bool = True,
    include_subcircuits: bool = True,
    format: str = "json",
    store_handle: bool = False,
) -> dict[str, Any]:
    """Compress an ADS netlist or handle into compact circuit parameters ('json' or ultra-compact 'tabular')."""
    return compress_nl(
        netlist_or_path,
        search_dirs=search_dirs,
        include_models=include_models,
        include_subcircuits=include_subcircuits,
        format=format,
        store_handle=store_handle,
    )


@server.tool()
def netlist_register(
    netlist_or_path: str,
    compact_format: str = "tabular",
    include_models: bool = True,
    include_subcircuits: bool = True,
    search_dirs: list[str] | None = None,
) -> dict[str, Any]:
    """Store an ADS netlist under a short handle (`nl_<12hex>`) and return compact topology/params to save tokens."""
    return register_nl(
        netlist_or_path,
        compact_format=compact_format,
        include_models=include_models,
        include_subcircuits=include_subcircuits,
        search_dirs=search_dirs,
    )


@server.tool()
def netlist_patch(
    netlist_or_handle: str,
    updates: dict[str, str | float | int],
) -> dict[str, Any]:
    """Patch VAR or Instance.Param values on a netlist handle and return only the new handle + parameter delta."""
    return patch_nl(netlist_or_handle, updates)


@server.tool()
def design_eval_step(
    netlist_or_handle: str,
    updates: dict[str, str | float | int] | None = None,
    sweep: dict[str, Any] | None = None,
    specs: list[dict[str, Any]] | None = None,
    erc_mode: str | None = "tb",
    compress_block: str | None = None,
    compress_axis: str | None = None,
    signals: list[str] | None = None,
    filters: dict[str, int | float | str] | None = None,
    max_curve_points: int = 16,
    band_hz: list[float] | None = None,
    data_dir: str | None = None,
    model_dirs: list[str] | None = None,
    timeout: float = 180.0,
) -> dict[str, Any]:
    """Single-turn atomic macro: Upfront Spec Validation -> Patch -> Static ERC -> 64-bit ADS Simulation -> Spec Scorecard."""
    return run_eval_step(
        netlist_or_handle,
        updates=updates,
        sweep=sweep,
        specs=specs,
        erc_mode=erc_mode,
        compress_block=compress_block,
        compress_axis=compress_axis,
        signals=signals,
        filters=filters,
        max_curve_points=max_curve_points,
        band_hz=band_hz,
        data_dir=data_dir,
        model_dirs=model_dirs,
        timeout=timeout,
    )


@server.tool()
def sensitivity_scan(
    netlist_or_handle: str,
    parameters: list[str | dict[str, Any]],
    objectives: list[dict[str, Any]],
    step_pct: float = 5.0,
    max_workers: int = 4,
    data_dir: str | None = None,
    model_dirs: list[str] | None = None,
    timeout_per_sim: float = 90.0,
) -> dict[str, Any]:
    """Run parallel +/-step_pct local perturbation simulations in the bridge and rank parameters by impact."""
    return run_sensitivity(
        netlist_or_handle,
        parameters,
        objectives,
        step_pct=step_pct,
        max_workers=max_workers,
        data_dir=data_dir,
        model_dirs=model_dirs,
        timeout_per_sim=timeout_per_sim,
    )


@server.tool()
def simulation_log_summary(session_id: str, job_id: str, max_items: int = 8) -> dict[str, Any]:
    """Distill a verbose hpeesofsim job log into a compact (<300-token) deduplicated error/warning fault card."""
    return read_log_summary(session_id, job_id, max_items=max_items)


@server.tool()
def dataset_compress(
    session_id: str,
    job_id: str | None = None,
    block: str | None = None,
    axis: str | None = None,
    signals: list[str] | None = None,
    max_curve_points: int = 24,
    band_hz: list[float] | None = None,
    load_ohm: float = 50.0,
    return_loss_spec_db: float = -10.0,
    export_touchstone: bool = False,
    vds_v: float | None = None,
    vds_signal: str | None = None,
    ids_signal: str | None = None,
    vout_signal: str | None = None,
    pin_dbm: float | None = None,
    filters: dict[str, int | float | str] | None = None,
    sweep_axis: str | None = None,
    pin_from_sweep_axis: bool = False,
) -> dict[str, Any]:
    """Compress simulation data (or an entire 2D ParamSweep via sweep_axis) into compact RF/circuit feature JSON."""
    return compress_ds(
        session_id,
        job_id,
        block=block,
        axis=axis,
        signals=signals,
        max_curve_points=max_curve_points,
        band_hz=band_hz,
        load_ohm=load_ohm,
        return_loss_spec_db=return_loss_spec_db,
        export_touchstone=export_touchstone,
        vds_v=vds_v,
        vds_signal=vds_signal,
        ids_signal=ids_signal,
        vout_signal=vout_signal,
        pin_dbm=pin_dbm,
        filters=filters,
        sweep_axis=sweep_axis,
        pin_from_sweep_axis=pin_from_sweep_axis,
    )


@server.tool()
def touchstone_compress(
    path: str,
    max_curve_points: int = 24,
    band_hz: list[float] | None = None,
    return_loss_spec_db: float = -10.0,
) -> dict[str, Any]:
    """Compress a Touchstone (.s1p/.s2p/.sNp) file into compact RF metrics and keypoints JSON using scikit-rf."""
    return compress_ts(
        path,
        max_curve_points=max_curve_points,
        band_hz=band_hz,
        return_loss_spec_db=return_loss_spec_db,
    )


@server.tool()
def optimize_netlist(
    netlist_or_path: str,
    parameters: list[dict[str, Any]],
    objectives: list[dict[str, Any]],
    method: str = "nelder_mead",
    max_evals: int = 30,
    tol: float = 1e-4,
    data_dir: str | None = None,
    model_dirs: list[str] | None = None,
    timeout_per_sim: float = 90.0,
) -> dict[str, Any]:
    """Run closed-loop automatic parameter optimization inside the bridge using NumPy/SciPy/scikit-rf + ADS."""
    return run_opt_netlist(
        netlist_or_path,
        parameters,
        objectives,
        method=method,
        max_evals=max_evals,
        tol=tol,
        data_dir=data_dir,
        model_dirs=model_dirs,
        timeout_per_sim=timeout_per_sim,
    )


@server.tool()
def dds_build(
    workspace_path: str,
    dataset_name: str,
    output_dds: str,
    pages: list[dict[str, Any]],
    timeout: float = 60.0,
) -> dict[str, Any]:
    """Build a native Keysight ADS Data Display (.dds) file with validated equations, plots, and tables."""
    return build_dds_file(workspace_path, dataset_name, output_dds, pages, timeout=timeout)


@server.tool()
def window_open(
    session_id: str,
    target: str,
    workspace_id: str | None = None,
    editable: bool = True,
) -> dict[str, Any]:
    """Open a schematic/layout view (`lib:cell:view`) or a `.dds` Data Display file inside a live ADS GUI session."""
    if target.lower().endswith(".dds"):
        return call(session_id, "window.open_dds", path=target)
    if not workspace_id:
        raise ValueError("Provide workspace_id when opening a schematic/layout/symbol design.")
    return call(session_id, "window.open_design", workspace_id=workspace_id, design=target, editable=editable)


@server.tool()
def gui_launch(
    workspace_path: str,
    design: str | None = None,
    dds_path: str | None = None,
    writable: bool = False,
    wait_timeout: float = 45.0,
) -> dict[str, Any]:
    """Launch foreground ADS 2026 GUI (detached), auto-start the live bridge, and open schematic & .dds windows."""
    return launch_ads_gui(
        workspace_path,
        design=design,
        dds_path=dds_path,
        writable=writable,
        wait_timeout=wait_timeout,
    )


@server.tool()
def headless_call(
    workspace_path: str,
    operation: str,
    arguments: dict[str, Any] | None = None,
    write: bool = False,
    create_workspace: bool = False,
    timeout: float = 120.0,
) -> dict[str, Any]:
    """Execute workspace/library/schematic/netlist/layout ops headlessly via standalone ADS Python (no GUI required)."""
    return run_headless(
        workspace_path,
        operation,
        write=write,
        create_workspace=create_workspace,
        timeout=timeout,
        **(arguments or {}),
    )


@server.tool()
def request_get(session_id: str, request_id: str) -> dict[str, Any]:
    """Look up a persisted write request, including after the ADS GUI session stops."""
    return read_request(session_id, request_id)


@server.tool()
def request_reconcile(session_id: str, request_id: str) -> dict[str, Any]:
    """Reconcile an interrupted or outcome_unknown write request against the live ADS GUI workspace."""
    return call(session_id, "request.reconcile", request_id=request_id)


# ---------------------------------------------------------------------------
# Token-Efficient Tool Registry Profiles
# ---------------------------------------------------------------------------

RF_CORE_TOOLS: frozenset[str] = frozenset({
    "bridge_status",
    "netlist_register",
    "netlist_patch",
    "netlist_compress",
    "design_eval_step",
    "optimize_netlist",
    "sensitivity_scan",
    "dataset_compress",
    "touchstone_compress",
    "simulation_log_summary",
    "dds_build",
    "pdk_search",
    "docs_search",
})

HEADLESS_TOOLS: frozenset[str] = RF_CORE_TOOLS | frozenset({
    "headless_call",
    "component_describe",
    "netlist_check",
    "simulate_netlist",
    "simulate_batch",
    "dataset_analyze",
})

MCP_PROFILES: dict[str, frozenset[str] | None] = {
    "full": None,          # All registered tools (GUI + headless + RF workflows)
    "headless": HEADLESS_TOOLS,
    "rf-core": RF_CORE_TOOLS,
}


def apply_mcp_profile(profile: str) -> list[str]:
    """Prune registered MCP tools according to *profile* to minimize per-turn tool-schema token tax."""
    key = profile.strip().lower()
    if key not in MCP_PROFILES:
        raise ValueError(f"Unsupported MCP profile {profile!r}. Choose from: {sorted(MCP_PROFILES)}")
    allowed = MCP_PROFILES[key]
    tool_dict = getattr(getattr(server, "_tool_manager", None), "_tools", None)
    if allowed is not None and isinstance(tool_dict, dict):
        for name in list(tool_dict.keys()):
            if name not in allowed:
                if hasattr(server, "remove_tool"):
                    server.remove_tool(name)
                else:
                    tool_dict.pop(name, None)
    return sorted(tool_dict.keys()) if isinstance(tool_dict, dict) else []


def main() -> None:
    parser = argparse.ArgumentParser(description="ADS Bridge MCP stdio server")
    parser.add_argument(
        "--profile",
        choices=sorted(MCP_PROFILES),
        default=os.environ.get("ADS_BRIDGE_MCP_PROFILE", "full"),
        help="Tool registry profile: 'full' (all tools), 'headless' (19 tools), or 'rf-core' (13 ultra-low-token tools).",
    )
    args = parser.parse_args()
    apply_mcp_profile(args.profile)
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
