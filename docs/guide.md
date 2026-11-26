# ADS Bridge User Guide

[Project home](../README.md) · [中文使用手册](guide.zh-CN.md) · [Changelog (Chinese)](../CHANGELOG.md)

## Installation

You need **Windows, Keysight ADS 2026 Update 0.1 with the 64-bit simulator, and a valid ADS simulation license**. Install **Python 3.11 or later** for the client. The bridge uses the Python interpreter bundled with ADS to open workspaces and run simulations.

```powershell
git clone https://github.com/Raven49963/ADS-Bridge.git
Set-Location ADS-Bridge
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install ".[mcp]"
.\.venv\Scripts\ads-bridge.exe status
```

If `status` cannot find ADS, point it to your installation, then run the check again.

```powershell
$env:HPEESOF_DIR = 'C:\Program Files\Keysight\ADS2026_Update0.1'
.\.venv\Scripts\ads-bridge.exe status
```

For RF data analysis and optimization without the MCP client dependencies, install `.[rf]`. If you downloaded the source archive from GitHub Releases, extract it and run the installation commands from the extracted directory.

## Run the RC example

```powershell
.\.venv\Scripts\python.exe -B .\verify_closed_loop.py
```

The script builds a filter schematic without opening the ADS window, exports and reads its netlist, changes the resistor, then compares the simulated response with the analytical result. Later steps check S-parameters, batch runs, optimization, GDSII export, Data Display, and MCP. If a live ADS session is reachable, the script also tries the GUI bridge. The [example schematic definition](../examples/rc_autowire.json) is a starting point for your own design.

## Run an existing netlist

Use an ADS `.ckt` export or a netlist you wrote. Check a testbench with `--mode tb` or a standalone device under test with `--mode dut`.

```powershell
.\.venv\Scripts\ads-bridge.exe netlist-inspect .\my_circuit.ckt
.\.venv\Scripts\ads-bridge.exe netlist-check .\my_circuit.ckt --mode tb
.\.venv\Scripts\ads-bridge.exe simulate .\my_circuit.ckt
.\.venv\Scripts\ads-bridge.exe --help
```

You can run the simulation from Python too.

```python
from pathlib import Path

import bridge

job = bridge.simulate_netlist(Path("my_circuit.ckt"))
print(job)
```

Use `headless_workflow` to create a library, inspect component pins, build or edit a schematic, and export it within one ADS Python process. To write to a workspace, pass its absolute path and set `write=True`. Set `create_workspace=True` to create a test workspace. See the [end-to-end script](../verify_closed_loop.py) for runnable steps.

## Connect an AI client

```powershell
.\setup_client.ps1
```

This script creates or reuses a Python environment and prints a `mcpServers.ads_bridge` configuration. In **Google Antigravity**, merge that entry into the raw MCP configuration. Other clients that run local MCP servers over standard input and output (stdio) can use the same command and arguments in their own configuration format.

For **Codex**, add this to the project's `.codex/config.toml`, replacing both example paths with your local paths.

```toml
[mcp_servers.ads_bridge]
command = 'C:\path\to\ADS-Bridge\.venv\Scripts\python.exe'
args = ['-B', 'C:\path\to\ADS-Bridge\mcp_server.py', '--profile', 'headless']
```

If you set `HPEESOF_DIR` or `ADS_BRIDGE_RUNTIME`, make sure the MCP process sees those settings. To connect to a live ADS session, use `full` and make sure the bridge and the add-on use the same runtime directory. Reload the client, then call `bridge_status`. Try `component_describe` or `simulate_netlist` in the headless profile, or use `session_list` to find a GUI session in the full profile.

| Profile | Tools | Use it for |
| --- | ---: | --- |
| `full` | 55 | GUI sessions, headless runs, and the complete RF toolkit |
| `headless` | 19 | Schematic work without the ADS window, batch simulation, and RF analysis |
| `rf-core` | 13 | Reusing netlists, checking specs, optimization, and compact summaries |

## Connect to the ADS window

In the ADS main window, open **Tools → Python Console** and register the add-on once.

```python
import sys

sys.path.insert(0, r"C:\path\to\ADS-Bridge")
import install_addon

install_addon.install()
```

Open **Tools → ADS Bridge** and select **Start read-only** to inspect a schematic or generate a netlist. To edit a test workspace, select **Start with write access to this workspace**. Use the CLI command `sessions` or the MCP tool `session_list` to find the session. Use its `session_id` and `workspace_id` to identify the design. The GUI bridge can open schematics and Data Display reports, and capture window images.

## Tool reference

| Task | Common tools |
| --- | --- |
| Inspect the ADS installation, docs, and components | `bridge_status`, `docs_search`, `pdk_list`, `pdk_search`, `pdk_describe`, `component_describe` |
| Create libraries and edit schematics | `library_create`, `design_list`, `design_read`, `design_build`, `design_update`, `design_check` |
| Generate, inspect, and update netlists | `netlist_generate`, `netlist_inspect`, `netlist_check`, `netlist_register`, `netlist_patch`, `netlist_compress` |
| Run and manage simulations | `simulate_netlist`, `simulate_batch`, `simulation_submit`, `simulation_status`, `simulation_cancel`, `simulation_result`, `simulation_log_summary` |
| Read datasets and analyze results | `dataset_describe`, `dataset_trace`, `dataset_table`, `dataset_analyze`, `dataset_compress`, `touchstone_compress` |
| Check specs and tune parameters | `design_eval_step`, `sensitivity_scan`, `optimize_netlist` |
| Work with layouts and reports | `layout_build`, `layout_read`, `layout_export_gds`, `dds_build`, `window_open` |
| Manage GUI sessions and resume work across clients | `gui_launch`, `session_inspect`, `session_capture`, `request_get`, `request_reconcile` |

See the [MCP tool definitions](../mcp_server.py) for parameters and [bridge.py](../bridge.py) for Python and CLI interfaces. The [two-client verification guide (Chinese)](../ANTIGRAVITY-ACCEPTANCE.md) shows how to use session and job IDs to pick up results from another client.

## Check results against specifications

1. Run electrical rule checks on the netlist. Check the part pins, ground reference, analysis settings, and external model paths.
2. After simulation, identify the dataset **block**, **axis**, and **signal**. Specify the load for RF output power. Power-added efficiency (PAE) also needs input power and DC supply information.
3. Use `design_eval_step` to apply changes, check the netlist, run the simulator, and score the results. Read `scorecard` for individual checks and `all_pass` for the overall result. For a sweep, inspect each sweep point or pass `filters` to check selected points.
4. For an equality requirement (`eq`), use `tolerance` to set an absolute tolerance in the metric's unit. A `pout_w` target of `0.01` and a tolerance of `0.0002` means 10 mW ±0.2 mW. If you use dBm, express the tolerance in dB.
5. To make the check reproducible, record the netlist, specs, ADS version, job ID, and raw data at key frequencies. A sensitivity scan can show which parameters are worth tuning before running the optimizer.

## Testing and troubleshooting

The end-to-end script returns JSON. Check `rc_closed_loop` for cutoff frequencies and the difference from the analytical result, and `mcp_stdio` for tool registration and data readback. If `live_gui_status` is `skipped`, start the ADS add-on and use the GUI verification command below.

```powershell
# Offline unit tests
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -v

# GUI test. Use a temporary ADS workspace with write access enabled.
.\.venv\Scripts\python.exe -B .\verify_live.py "your-session-id" "C:\path\to\test_wrk"
```

- **ADS or its license is missing.** Check `status`, `HPEESOF_DIR`, and the ADS license settings.
- **The GUI session is missing.** Start the add-on and check that ADS and the client use the same runtime directory. The default is `.ads_bridge` in your user directory. Use `sessions` to check.
- **A simulation fails.** Read the job status, `simulation_log_summary`, or the full log. Check model directories, analysis settings, and convergence.
- **A dataset is empty or has several sweep axes.** Use `dataset_describe` to find the correct block, axis, and signal, then select the outer sweep coordinates before reading raw samples.
