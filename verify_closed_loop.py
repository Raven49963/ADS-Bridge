"""End-to-end closed-loop verification of ADS Bridge 2.0 on Windows 64-bit ADS 2026 Update 0.1.

Covers the complete scriptable engineering lifecycle without requiring manual GUI clicks:
1. Environment doctor, offline ADS documentation search, and built-in/PDK library discovery.
2. Component symbol pin/parameter introspection (`component.describe`).
3. Parameterized schematic creation using terminal-to-terminal auto-wiring, pins, and VAR blocks.
4. OpenAccess connectivity + ADS netlist semantic comprehension (`design.check` + `netlist.inspect`).
5. 64-bit simulation (`CircuitSimulator`), waveform analytics (`-3 dB` cutoff & `at` interpolation),
   incremental parameter tuning (`3 kOhm -> 6 kOhm`), and causal cutoff frequency shift verification.
6. Two-port RF S-parameter schematic build, simulation, passivity/reciprocity verification,
   parallel batch simulation (`simulate_batch`), negative fault injection, and GDSII layout export.
7. MCP stdio protocol purity (zero stdout pollution, 43 tools) and live ADS 2.x GUI session interop (if running).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
from pathlib import Path
import shutil
import sys
import tempfile
from typing import Any
import uuid

import bridge


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(f"VERIFICATION FAILED: {message}")


def gds_record_types(data: bytes) -> list[int]:
    types = []
    pos = 0
    while pos + 4 <= len(data):
        size = int.from_bytes(data[pos:pos + 2], "big")
        require(size >= 4 and size % 2 == 0 and pos + size <= len(data), "Malformed GDSII record.")
        kind = data[pos + 2]
        types.append(kind)
        if kind == 4:
            return types
        pos += size
    raise RuntimeError("GDSII ENDLIB record missing.")


def _version_tuple(version_str: str | None) -> tuple[int, ...]:
    if not isinstance(version_str, str):
        return (0,)
    parts = []
    for item in version_str.split("."):
        if item.isdigit():
            parts.append(int(item))
        else:
            break
    return tuple(parts) or (0,)


async def verify_mcp_stdio(
    mcp_python: Path,
    runtime_path: Path,
    sample_job_id: str,
    live_session: dict[str, Any] | None,
) -> dict[str, Any]:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    env = os.environ.copy()
    env["ADS_BRIDGE_RUNTIME"] = str(runtime_path)
    params = StdioServerParameters(
        command=str(mcp_python),
        args=["-B", str(Path(__file__).with_name("mcp_server.py"))],
        env=env,
    )
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as client:
            await client.initialize()
            listed = await client.list_tools()
            tool_names = {t.name for t in listed.tools}
            expected_core = {
                "bridge_status", "docs_search", "pdk_list", "pdk_describe", "pdk_search",
                "component_describe", "design_build", "design_check", "design_update",
                "netlist_inspect", "netlist_check", "netlist_compress",
                "netlist_register", "netlist_patch", "design_eval_step",
                "sensitivity_scan", "simulation_log_summary",
                "simulate_netlist", "simulate_batch", "simulation_cancel",
                "dataset_analyze", "dataset_compress", "touchstone_compress",
                "optimize_netlist", "dds_build", "window_open",
                "headless_call", "request_reconcile",
            }
            require(expected_core <= tool_names, f"Missing MCP tools: {expected_core - tool_names}")
            require(len(tool_names) >= 55, f"Expected at least 55 MCP tools in full profile, got {len(tool_names)}")

            st = await client.call_tool("bridge_status", {})
            require(not st.isError and st.structuredContent["ok"], f"MCP bridge_status failed: {st}")

            an = await client.call_tool(
                "dataset_analyze",
                {
                    "session_id": "standalone",
                    "job_id": sample_job_id,
                    "block": "AC1.AC",
                    "axis": "freq",
                    "signals": ["VOUT"],
                    "at": [26.5258238, 1000.0, 1e9],
                },
            )
            require(not an.isError, f"MCP dataset_analyze failed: {an}")
            samples_at = an.structuredContent["signals"]["VOUT"]["samples_at"]
            require(samples_at[0]["in_range"] is True and samples_at[2]["in_range"] is False,
                    f"MCP dataset_analyze did not flag out-of-range sample at 1 GHz: {samples_at}")

            live_echo = None
            live_gui_status = "skipped (no reachable ADS GUI session)"
            if live_session is not None:
                live_session_id = live_session["session_id"]
                live_bv = live_session.get("bridge_version")
                require(
                    _version_tuple(live_bv) >= (2, 0, 0),
                    f"Live ADS GUI session {live_session_id} runs legacy bridge_version={live_bv!r}; reload 2.0+ addon.",
                )
                insp = await client.call_tool(
                    "session_inspect",
                    {"session_id": live_session_id, "challenge": "mcp-live-verify"},
                )
                require(
                    not insp.isError and insp.structuredContent["challenge"] == "mcp-live-verify"
                    and insp.structuredContent["on_gui_thread"],
                    f"MCP live GUI session_inspect failed: {insp}",
                )
                live_echo = insp.structuredContent["workspace_path"]
                ws_id = insp.structuredContent.get("workspace_id")
                if ws_id:
                    comp = await client.call_tool(
                        "component_describe",
                        {"session_id": live_session_id, "workspace_id": ws_id, "master": "ads_rflib:R:symbol"},
                    )
                    require(
                        not comp.isError and len(comp.structuredContent.get("terminals", ())) == 2,
                        f"Live GUI 2.0 component_describe failed: {comp}",
                    )
                    live_gui_status = f"verified_v{live_bv} (session_inspect + component_describe)"
                else:
                    rec = await client.call_tool(
                        "request_reconcile",
                        {"session_id": live_session_id, "request_id": "0" * 32},
                    )
                    # A 2.0+ session dispatches request.reconcile and reports request_not_found (never unknown_operation)
                    rec_text = str(rec)
                    require("unknown_operation" not in rec_text, f"Live GUI session lacks 2.0 request.reconcile: {rec}")
                    live_gui_status = f"verified_v{live_bv} (session_inspect + request_reconcile)"

            return {
                "published_tool_count": len(tool_names),
                "mcp_dataset_fc_hz": an.structuredContent["signals"]["VOUT"]["crossings_3db"][0],
                "live_gui_status": live_gui_status,
                "live_gui_workspace": live_echo,
            }


async def verify_mcp_rf_core_profile(mcp_python: Path, runtime_path: Path) -> int:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    env = os.environ.copy()
    env["ADS_BRIDGE_RUNTIME"] = str(runtime_path)
    params = StdioServerParameters(
        command=str(mcp_python),
        args=["-B", str(Path(__file__).with_name("mcp_server.py")), "--profile", "rf-core"],
        env=env,
    )
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as client:
            await client.initialize()
            listed = await client.list_tools()
            names = {t.name for t in listed.tools}
            require(len(names) == 13 and "design_eval_step" in names and "sensitivity_scan" in names,
                    f"Unexpected rf-core MCP tool set ({len(names)}): {sorted(names)}")
            return len(names)


def verify_structured_parameters(workspace: Path) -> dict[str, Any]:
    design = "loop_lib:verify_sdd:schematic"
    master = "ads_datacmps:SDD3P:symbol"
    initial = [["1", "0", "_v1/130"], ["2", "0", "_v2/50"], ["3", "0", "_v3/75"]]
    updated = [["1", "0", "_v1/110"], ["2", "0", "_v2/60"], ["3", "0", "_v3/90"]]

    def instance(rows: Any) -> dict[str, Any]:
        return {"name": "EQ", "master": master, "at": [0, 0], "parameters": {"SDD": rows}}

    def edit(rows: Any) -> dict[str, Any]:
        return {"op": "set_param", "instance": "EQ", "parameters": {"SDD": rows}}

    def read_value() -> list[list[str]]:
        read = bridge.headless_call(workspace, "design.read", design=design)
        return read["instances"][0]["parameters"]["SDD"]

    def rejected(op: str, **args: Any) -> None:
        try:
            bridge.headless_call(workspace, op, write=True, design=design, **args)
        except bridge.BridgeError as exc:
            require(exc.code == "invalid_parameter_shape", f"Unexpected error for incomplete SDD: {exc.code}")
        else:
            raise RuntimeError("Incomplete SDD parameter payload was accepted.")

    metadata = bridge.headless_call(workspace, "component.describe", master=master)
    shape = next(p["input_shape"] for p in metadata["parameters"] if p["name"] == "SDD")
    require(shape == [["string"] * 3] * 3, f"SDD3P metadata shape is incorrect: {shape}")
    build = bridge.headless_call(workspace, "design.build", write=True, design=design, instances=[instance(initial)], wires=[])
    require(read_value() == initial, "The three initial SDD rows did not persist.")
    rejected("design.update", revision=build["revision"], ops=[edit(updated), edit(updated[:2])])
    require(read_value() == initial, "The failed update saved an earlier edit from the same transaction.")
    rejected("design.build", instances=[instance("_v1")], wires=[], replace=True)
    require(read_value() == initial, "The failed replacement discarded the original schematic.")
    result = bridge.headless_call(workspace, "design.update", write=True, design=design, revision=build["revision"], ops=[edit(updated)])
    change = result["applied"][0]["changes"][0]
    require(change["old_value"] == initial and change["value"] == updated and read_value() == updated,
            f"Incorrect structured write receipt: {change}")
    netlist = bridge.headless_call(workspace, "netlist.generate", design=design)["text"]
    require(all(f"I[{port},{weight}]={expr}" in netlist for port, weight, expr in updated),
            "ADS netlist is missing one or more updated equations.")
    return {"round_trip_rows": len(updated), "update_rollback": True, "replacement_rollback": True}


def run_closed_loop(mcp_python: Path, keep_temp: bool = False, session_id: str | None = None) -> dict[str, Any]:
    report: dict[str, Any] = {}

    # 1. Environment, Docs & PDK checks
    status = bridge.bridge_status()
    require(status["ok"], f"ADS environment check failed: {status}")
    report["ads_version"] = status["ads_version"]
    report["simulator_banner"] = status["simulator_banner"]

    docs = bridge.docs_search("Transaction", limit=5)
    require(docs["total_matches"] >= 1, "Installed ADS documentation search returned 0 matches for 'Transaction'.")
    rflib = bridge.pdk_describe("ads_rflib")
    require(rflib["cell_count"] >= 10, f"Unexpected ads_rflib cell count: {rflib}")
    mlin_hits = bridge.pdk_search("MLIN", limit=10)
    require(any(m["cell"] == "MLIN" for m in mlin_hits["matches"]), "PDK search did not find MLIN.")
    report["indexed_docs"] = docs["indexed_documents"]
    report["ads_rflib_cells"] = rflib["cell_count"]

    # Check if a live ADS GUI session is currently active in the configured runtime
    reachable_sessions = [s for s in status["sessions"] if s.get("reachable")]
    live_session: dict[str, Any] | None = None
    if session_id is not None:
        matched = [s for s in reachable_sessions if s["session_id"] == session_id]
        require(bool(matched), f"Requested live ADS GUI session {session_id} is not reachable.")
        live_session = matched[0]
    else:
        v2_sessions = [s for s in reachable_sessions if _version_tuple(s.get("bridge_version")) >= (2, 0, 0)]
        if v2_sessions:
            live_session = v2_sessions[0]
        elif reachable_sessions:
            report["legacy_gui_sessions_ignored"] = [
                {"session_id": s["session_id"], "bridge_version": s.get("bridge_version")}
                for s in reachable_sessions
            ]

    # Use an isolated temp workspace directory for full closed-loop design -> verification
    temp_root = Path(tempfile.mkdtemp(prefix="ads_bridge_v2_loop_"))
    try:
        ws_path = temp_root / "closed_loop_wrk"
        examples_dir = Path(__file__).with_name("examples")
        rc_manifest = bridge.read_json(examples_dir / "rc_autowire.json")
        sp_manifest = bridge.read_json(examples_dir / "series_resistor_sp.json")
        lay_manifest = bridge.read_json(examples_dir / "layout.json")

        # 2. Headless Workspace + Library + Component Introspection + Auto-Wired Schematic Build
        wf = bridge.headless_workflow(
            ws_path,
            [
                {"op": "library.create", "args": {"name": "loop_lib", "layout_technology": True}},
                {"op": "component.describe", "args": {"master": "ads_rflib:R:symbol"}},
                {"op": "component.describe", "args": {"master": "ads_rflib:C:symbol"}},
                {"op": "design.build", "args": {"design": "loop_lib:rc_lpf:schematic", **rc_manifest}},
                {"op": "design.check", "args": {"design": "loop_lib:rc_lpf:schematic", "mode": "tb"}},
                {"op": "design.read", "args": {"design": "loop_lib:rc_lpf:schematic"}},
                {"op": "netlist.generate", "args": {"design": "loop_lib:rc_lpf:schematic", "inspect": True}},
            ],
            write=True,
            create_workspace=True,
        )
        steps = wf["steps"]
        r_meta, c_meta, build_res, check_res, read_res, nl_3k = steps[1], steps[2], steps[3], steps[4], steps[5], steps[6]

        require(len(r_meta["terminals"]) == 2 and any(p["name"] == "R" for p in r_meta["parameters"]),
                f"Component introspection incomplete for R: {r_meta}")
        require(len(c_meta["terminals"]) == 2 and any(p["name"] == "C" for p in c_meta["parameters"]),
                f"Component introspection incomplete for C: {c_meta}")
        require(build_res["state"] == "saved" and check_res["ok"] and check_res["error_count"] == 0,
                f"Auto-wired RC schematic check failed: {check_res}")
        require(len(read_res["ports"]) == 1 and read_res["ports"][0]["name"] == "VOUT",
                f"Schematic port pin VOUT missing in read_design: {read_res['ports']}")

        # 3. Confirm Netlist is AI-Readable & Analytically Understood (3 kOhm -> fc ~ 53.0516 Hz)
        parsed_3k = nl_3k["parsed"]
        fc_3k_theory = 1.0 / (2.0 * math.pi * 3000.0 * 1e-6)
        require(parsed_3k["circuit_insights"][0]["archetype"] == "rc_lowpass_1st_order",
                f"Netlist AI parser did not detect RC low-pass: {parsed_3k}")
        require(abs(parsed_3k["circuit_insights"][0]["cutoff_hz"] - fc_3k_theory) < 1e-6,
                f"Netlist cutoff mismatch: {parsed_3k['circuit_insights'][0]}")
        report["netlist_3k_ai_summary"] = parsed_3k["ai_summary"]

        # Simulate 3 kOhm netlist and verify waveform + -3 dB cutoff
        sim_3k = bridge.simulate_netlist(nl_3k["text"], block="AC1.AC", axis="freq", variable="VOUT")
        require(sim_3k["state"] == "succeeded", f"3 kOhm simulation failed: {sim_3k}")
        trace_3k = bridge.job_result("standalone", sim_3k["job_id"])
        err_3k = max(
            abs(complex(re_v, im_v) - 1.0 / (1.0 + 2j * math.pi * f * 0.003))
            for f, re_v, im_v in trace_3k["samples"]
        )
        require(len(trace_3k["samples"]) == 31 and err_3k < 1e-6, f"3 kOhm AC waveform error {err_3k}")
        ana_3k = bridge.dataset_analyze("standalone", sim_3k["job_id"], "AC1.AC", "freq", signals=["VOUT"], at=[fc_3k_theory])
        fc_3k_meas = ana_3k["signals"]["VOUT"]["crossings_3db"][0]
        require(abs(fc_3k_meas - fc_3k_theory) / fc_3k_theory < 0.02, f"Measured 3k fc={fc_3k_meas} vs {fc_3k_theory}")

        # 4. Incremental Schematic Edit (VAR1 R_val: 3 kOhm -> 6 kOhm) + Revision Guard + Re-Verification
        upd = bridge.headless_workflow(
            ws_path,
            [
                {
                    "op": "design.update",
                    "args": {
                        "design": "loop_lib:rc_lpf:schematic",
                        "revision": nl_3k["revision"],
                        "ops": [{"op": "set_var", "name": "VAR1", "variables": {"R_val": "6 kOhm"}}],
                    },
                },
                {"op": "netlist.generate", "args": {"design": "loop_lib:rc_lpf:schematic", "inspect": True}},
            ],
            write=True,
        )
        nl_6k = upd["steps"][1]
        fc_6k_theory = 1.0 / (2.0 * math.pi * 6000.0 * 1e-6)
        require(abs(nl_6k["parsed"]["circuit_insights"][0]["cutoff_hz"] - fc_6k_theory) < 1e-6,
                "Updated 6 kOhm netlist cutoff did not halve as expected.")

        # Verify stale revision is rejected
        try:
            bridge.headless_call(
                ws_path,
                "design.update",
                write=True,
                design="loop_lib:rc_lpf:schematic",
                revision=nl_3k["revision"],
                ops=[{"op": "set_var", "name": "VAR1", "variables": {"R_val": "9 kOhm"}}],
            )
        except bridge.BridgeError as exc:
            require(exc.code == "design_changed", f"Expected design_changed, got {exc.code}")
        else:
            raise RuntimeError("Stale schematic revision was not rejected!")

        sim_6k = bridge.simulate_netlist(nl_6k["text"], block="AC1.AC", axis="freq", variable="VOUT")
        require(sim_6k["state"] == "succeeded", f"6 kOhm simulation failed: {sim_6k}")
        trace_6k = bridge.job_result("standalone", sim_6k["job_id"])
        err_6k = max(
            abs(complex(re_v, im_v) - 1.0 / (1.0 + 2j * math.pi * f * 0.006))
            for f, re_v, im_v in trace_6k["samples"]
        )
        require(err_6k < 1e-6, f"6 kOhm AC waveform error {err_6k}")
        ana_6k = bridge.dataset_analyze("standalone", sim_6k["job_id"], "AC1.AC", "freq", signals=["VOUT"], at=[fc_6k_theory])
        fc_6k_meas = ana_6k["signals"]["VOUT"]["crossings_3db"][0]
        require(abs(fc_6k_meas - fc_6k_theory) / fc_6k_theory < 0.02, f"Measured 6k fc={fc_6k_meas} vs {fc_6k_theory}")
        report["rc_closed_loop"] = {
            "fc_3k_hz": round(fc_3k_meas, 4),
            "fc_6k_hz": round(fc_6k_meas, 4),
            "max_complex_err_3k_V": err_3k,
            "max_complex_err_6k_V": err_6k,
        }

        # 5. Two-Port S-Parameter Network + Batch Simulation + Fault Injection + Layout/GDSII
        sp_wf = bridge.headless_workflow(
            ws_path,
            [
                {"op": "design.build", "args": {"design": "loop_lib:sp_2port:schematic", **sp_manifest}},
                {"op": "netlist.generate", "args": {"design": "loop_lib:sp_2port:schematic", "inspect": True}},
                {"op": "layout.build", "args": {"design": "loop_lib:geom:layout", **lay_manifest}},
                {"op": "layout.read", "args": {"design": "loop_lib:geom:layout"}},
                {"op": "layout.export_gds", "args": {"design": "loop_lib:geom:layout"}},
            ],
            write=True,
        )
        sp_nl = sp_wf["steps"][1]
        lay_read = sp_wf["steps"][3]
        gds_out = sp_wf["steps"][4]

        batch = bridge.simulate_batch([nl_6k["text"], sp_nl["text"]], max_workers=2)
        require(batch["succeeded"] == 2, f"Batch simulation did not succeed 2/2: {batch}")
        sp_job_id = batch["jobs"][1]["job_id"]
        sp_ana = bridge.dataset_analyze("standalone", sp_job_id, "SP1.SP", "freq", signals=["S[1,1]", "S[2,1]"], at=[200e6])
        require(sp_ana["sparam_2port"]["is_passive"] and sp_ana["sparam_2port"]["is_reciprocal"],
                f"2-port passivity/reciprocity failed: {sp_ana['sparam_2port']}")
        require(abs(sp_ana["signals"]["S[2,1]"]["samples_at"][0]["real"] - 2.0 / 3.0) < 1e-6,
                "Interpolated S[2,1] at 200 MHz did not match 2/3.")

        # Negative fault injection: bad netlist must fail cleanly with structured diagnostics
        bad_sim = bridge.simulate_netlist("R:R1 N1 0 R=UNDEFINED_VAR\nAC:AC1 Start=1 Hz Stop=10 Hz Step=1 Hz\n")
        require(bad_sim["state"] == "failed", f"Broken netlist unexpectedly succeeded: {bad_sim}")

        # Verify GDSII binary structure
        gds_bytes = Path(gds_out["artifact"]).read_bytes()
        gds_records = gds_record_types(gds_bytes)
        require(lay_read["bbox"]["right"] == 30.0 and gds_records.count(8) == 2,
                "Layout bbox or GDSII BOUNDARY record count mismatch.")
        report["sparam_and_layout"] = {
            "batch_runs": f"{batch['succeeded']}/{batch['total']}",
            "sparam_passive": sp_ana["sparam_2port"]["is_passive"],
            "sparam_reciprocal": sp_ana["sparam_2port"]["is_reciprocal"],
            "gds_bytes": gds_out["bytes"],
            "negative_sim_state": bad_sim["state"],
            "negative_sim_code": bad_sim.get("code"),
        }

        report["structured_parameters"] = verify_structured_parameters(ws_path)

        # 6. 2.1.0 RF Compression (Netlist + scikit-rf Dataset/Touchstone), Closed-Loop Optimizer & Native DDS
        nl_comp = bridge.netlist_compress(nl_3k["text"])
        require(
            nl_comp["stats"]["vs_inspect_ratio"] >= 2.0
            and any(p["ref"] == "VAR:R_val" for p in nl_comp["tunable_params"]),
            f"netlist_compress missing expected tunable_params or stats: {nl_comp}",
        )

        sp_comp = bridge.dataset_compress(
            "standalone",
            sp_job_id,
            block="SP1.SP",
            axis="freq",
            max_curve_points=8,
            export_touchstone=True,
        )
        s2p_export = Path(sp_comp["touchstone_file"])
        require(
            sp_comp["kind"] == "sparameter"
            and sp_comp["engine"].get("scikit_rf") is not None
            and sp_comp["network_properties"]["is_passive"] is True
            and sp_comp["network_properties"]["is_reciprocal"] is True
            and s2p_export.is_file(),
            f"scikit-rf dataset_compress failed: {sp_comp}",
        )
        ts_comp = bridge.touchstone_compress(s2p_export, max_curve_points=8)
        s21_db_expected = 20.0 * math.log10(2.0 / 3.0)
        require(
            ts_comp["kind"] == "sparameter"
            and ts_comp["network_properties"]["n_ports"] == 2
            and abs(ts_comp["parameters"]["S[2,1]"]["max_db"] - s21_db_expected) < 1e-3,
            f"touchstone_compress failed: {ts_comp}",
        )

        ac_comp = bridge.dataset_compress(
            "standalone",
            sim_6k["job_id"],
            block="AC1.AC",
            axis="freq",
            signals=["VOUT"],
            max_curve_points=12,
        )
        require(
            ac_comp["kind"] == "ac"
            and len(ac_comp["signals"]["VOUT"]["keypoints_db"]) <= 14
            and ac_comp["compression"]["raw_samples"] == 31,
            f"AC dataset_compress failed: {ac_comp}",
        )

        # Closed-loop derivative-free optimization: tune R_val to move RC -3 dB cutoff to 100 Hz
        opt_res = bridge.optimize_netlist(
            nl_3k["text"],
            parameters=[{"ref": "VAR:R_val", "min": 500.0, "max": 5000.0, "init": 3000.0, "unit": "Ohm", "scale": "log"}],
            objectives=[
                {
                    "id": "fc_target",
                    "block": "AC1.AC",
                    "axis": "freq",
                    "signal": "VOUT",
                    "metric": "cutoff_3db_hz",
                    "op": "eq",
                    "target": 100.0,
                    "weight": 1.0,
                }
            ],
            method="nelder_mead",
            max_evals=14,
            tol=1e-2,
        )
        r_target_theory = 1.0 / (2.0 * math.pi * 100.0 * 1e-6)  # ~1591.55 Ohm
        r_opt = opt_res["best_parameters"]["VAR:R_val"]["value"]
        fc_opt = opt_res["best_metrics"]["fc_target"]["measured"]
        require(
            opt_res["ok"]
            and opt_res["best_cost"] < opt_res["initial_cost"]
            and abs(fc_opt - 100.0) < 3.0
            and abs(r_opt - r_target_theory) / r_target_theory < 0.05
            and Path(opt_res["best_netlist_path"]).is_file(),
            f"optimize_netlist did not converge to fc=100 Hz: {opt_res}",
        )

        # Native .dds Data Display builder
        ds_6k_src = bridge.job_directory("standalone", sim_6k["job_id"]) / Path(sim_6k["dataset"]).name
        ws_data_dir = ws_path / "data"
        ws_data_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ds_6k_src, ws_data_dir / "rc_6k_verify.ds")
        dds_out = bridge.dds_build(
            ws_path,
            "rc_6k_verify",
            "rc_6k_report.dds",
            pages=[
                {
                    "name": "RC_Response",
                    "equations": [{"name": "vout_db", "expr": "dB(VOUT)"}],
                    "plots": [{"title": "RC LPF |VOUT| (dB)", "traces": ["vout_db"]}],
                }
            ],
        )
        dds_path = Path(dds_out["dds_path"])
        require(
            dds_out["ok"] and dds_path.is_file() and dds_path.stat().st_size > 200,
            f"dds_build failed: {dds_out}",
        )

        report["rf_v21_upgrade"] = {
            "netlist_compression_ratio": nl_comp["stats"]["compression_ratio"],
            "scikit_rf_version": sp_comp["engine"]["scikit_rf"],
            "sparam_compression_ratio": sp_comp["compression"]["compression_ratio"],
            "ac_keypoints": len(ac_comp["signals"]["VOUT"]["keypoints_db"]),
            "opt_evaluations": opt_res["evaluations"],
            "opt_best_R_ohm": round(r_opt, 2),
            "opt_best_fc_hz": round(fc_opt, 2),
            "opt_cost_reduction": round(opt_res["initial_cost"] / max(opt_res["best_cost"], 1e-9), 2),
            "dds_bytes": dds_path.stat().st_size,
        }

        # 7. 2.2.0 Token-Efficiency Layer: Netlist Handles + Tabular Compression + Eval Macro + Sensitivity + Log Distill
        reg = bridge.netlist_register(nl_3k["text"], compact_format="tabular")
        nl_handle = reg["netlist_id"]
        require(
            nl_handle.startswith("nl_")
            and reg["compact"]["schema"] == "ads_bridge.netlist_tabular.v2"
            and reg["compact"]["stats"]["compact_chars"] < nl_comp["stats"]["compact_chars"],
            f"netlist_register tabular compression failed: {reg}",
        )
        patched = bridge.netlist_patch(nl_handle, {"VAR:R_val": "2 kOhm"})
        require(
            patched["delta"]["changed"]["VAR:R_val"][1] == "2 kOhm",
            f"netlist_patch delta mismatch: {patched}",
        )
        ev_step = bridge.design_eval_step(
            patched["netlist_id"],
            updates={"VAR:R_val": "1.6 kOhm"},
            specs=[
                {
                    "id": "fc_spec",
                    "block": "AC1.AC",
                    "axis": "freq",
                    "signal": "VOUT",
                    "metric": "cutoff_3db_hz",
                    "op": "ge",
                    "target": 90.0,
                }
            ],
        )
        require(
            ev_step["ok"] and ev_step["all_pass"] and ev_step["scorecard"][0][3] == "PASS",
            f"design_eval_step failed: {ev_step}",
        )
        sens = bridge.sensitivity_scan(
            patched["netlist_id"],
            ["VAR:R_val"],
            [{"id": "fc", "block": "AC1.AC", "axis": "freq", "signal": "VOUT", "metric": "cutoff_3db_hz", "op": "eq", "target": 80.0}],
            step_pct=5.0,
        )
        require(
            sens["ok"] and sens["evaluations"] == 3 and sens["ranked_params"][0]["effects"]["fc"]["delta_per_1pct"] < 0.0,
            f"sensitivity_scan failed: {sens}",
        )
        bad_log_card = bridge.job_log_summary("standalone", bad_sim["job_id"])
        require(
            bad_log_card["error_count"] >= 1 and len(bad_log_card["hints"]) >= 1,
            f"job_log_summary failed to distill bad_sim fault: {bad_log_card}",
        )

        # Native multi-line ADS RC netlist parameter sweep + re-injection override (no duplicate SW_BRIDGE_CTRL)
        sw_sim = bridge.simulate_netlist(
            nl_3k["text"],
            sweep={"var": "R_val", "start": "1 kOhm", "stop": "5 kOhm", "lin": 5},
        )
        require(sw_sim["state"] == "succeeded", f"Multi-line native ParamSweep failed: {sw_sim}")
        sw_cat = bridge.dataset_catalog("standalone", sw_sim["job_id"])
        sw_axes = sw_cat["blocks"][0]["axes"]
        require(
            "R_val" in sw_axes and "freq" in sw_axes,
            f"ParamSweep dataset missing R_val/freq axes: {sw_cat}",
        )
        import netlist_parser
        sw_once_nl = netlist_parser.inject_sweep_plan(nl_3k["text"], var="R_val", start="1 kOhm", stop="5 kOhm", lin=5)
        sw_sim2 = bridge.simulate_netlist(
            sw_once_nl,
            sweep={"var": "R_val", "start": "2 kOhm", "stop": "10 kOhm", "lin": 5},
        )
        require(sw_sim2["state"] == "succeeded", f"Re-injected ParamSweep failed: {sw_sim2}")

        # Reject physically mismatched metric (S-parameter metric s_db_at requested on AC voltage VOUT)
        try:
            bridge.design_eval_step(
                nl_handle,
                specs=[
                    {"id": "bad_s11", "block": "AC1.AC", "axis": "freq", "signal": "VOUT", "metric": "s_db_at", "at_hz": 1000.0, "op": "le", "target": -10.0}
                ],
            )
        except bridge.BridgeError as exc:
            require(exc.code == "invalid_argument", f"Unexpected error code for AC-as-S11: {exc.code}")
        else:
            raise RuntimeError("s_db_at on AC voltage VOUT was not rejected!")

        # Dual-frequency spec verification (1 Hz PASS, 1 kHz FAIL -> overall all_pass must be False)
        dual_eval = bridge.design_eval_step(
            nl_handle,
            specs=[
                {"id": "gain_1khz", "block": "AC1.AC", "axis": "freq", "signal": "VOUT", "metric": "db20_at", "at_hz": 1000.0, "op": "ge", "target": -1.0},
                {"id": "gain_1hz", "block": "AC1.AC", "axis": "freq", "signal": "VOUT", "metric": "db20_at", "at_hz": 1.0, "op": "ge", "target": -1.0},
            ],
        )
        require(
            dual_eval["ok"] and dual_eval["all_pass"] is False
            and dual_eval["scorecard"][0][3] == "FAIL" and dual_eval["scorecard"][1][3] == "PASS",
            f"Dual-frequency scorecard failed: {dual_eval}",
        )

        # 8. MCP stdio end-to-end protocol purity (full + rf-core profile) + live GUI check
        mcp_report = asyncio.run(verify_mcp_stdio(mcp_python, bridge.runtime_dir(), sim_6k["job_id"], live_session))
        rf_core_count = asyncio.run(verify_mcp_rf_core_profile(mcp_python, bridge.runtime_dir()))
        mcp_report["rf_core_tool_count"] = rf_core_count
        report["token_v22_upgrade"] = {
            "tabular_compact_chars": reg["compact"]["stats"]["compact_chars"],
            "json_compact_chars": nl_comp["stats"]["compact_chars"],
            "eval_step_scorecard": ev_step["scorecard"],
            "sensitivity_delta_per_1pct": sens["ranked_params"][0]["effects"]["fc"]["delta_per_1pct"],
            "distilled_error_count": bad_log_card["error_count"],
            "mcp_full_tools": mcp_report["published_tool_count"],
            "mcp_rf_core_tools": rf_core_count,
        }
        report["mcp_stdio"] = mcp_report
        return report
    finally:
        if not keep_temp:
            shutil.rmtree(temp_root, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mcp-python",
        type=Path,
        default=Path(sys.executable),
        help="Python interpreter with 'mcp' SDK installed for stdio verification.",
    )
    parser.add_argument(
        "--session-id",
        default=None,
        help="Explicitly bind and verify a specific live ADS 2.x GUI session ID.",
    )
    parser.add_argument("--keep-temp", action="store_true")
    args = parser.parse_args()
    summary = run_closed_loop(args.mcp_python.resolve(), keep_temp=args.keep_temp, session_id=args.session_id)
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
