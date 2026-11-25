"""Verify the live ADS bridge against a newly created, write-enabled RC fixture."""

import argparse
import asyncio
import math
from pathlib import Path
import sys
import time
import uuid

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from bridge import BridgeError, call, job_get, runtime_dir


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def wait_for_job(session_id, job_id):
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        status = job_get(session_id, job_id)
        if status["state"] not in {"starting", "running"}:
            require(status["state"] == "succeeded", str(status))
            return
        time.sleep(0.25)
    raise TimeoutError(f"Job {job_id} did not finish. Inspect its logs.")


async def read_with_mcp(session_id, job_id, workspace_id):
    settings = StdioServerParameters(
        command=sys.executable,
        args=["-B", str(Path(__file__).with_name("mcp_server.py"))],
        env={"ADS_BRIDGE_RUNTIME": str(runtime_dir())},
    )
    async with stdio_client(settings) as (reader, writer):
        async with ClientSession(reader, writer) as client:
            await client.initialize()
            tools = await client.list_tools()
            require({"session_inspect", "simulation_submit", "simulation_result"} <= {tool.name for tool in tools.tools},
                    "Expected MCP tools were not published.")
            inspected = await client.call_tool("session_inspect", {"session_id": session_id, "challenge": "mcp-test"})
            require(not inspected.isError and inspected.structuredContent["workspace_id"] == workspace_id,
                    "MCP returned the wrong workspace.")
            result = await client.call_tool("simulation_result", {"session_id": session_id, "job_id": job_id})
            require(not result.isError, str(result))
            return result.structuredContent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session_id")
    parser.add_argument("fixture", type=Path, help="Path of the disposable probe_wrk. R1 will be saved as 6 kOhm.")
    args = parser.parse_args()
    sid = args.session_id
    inspected = call(sid, "session.inspect", challenge="rc-smoke")
    require(inspected["challenge"] == "rc-smoke" and inspected["in_ads_gui"] and inspected["on_gui_thread"],
            "ADS did not execute the challenge on its GUI thread.")
    require(inspected["write_enabled"] and Path(inspected["workspace_path"]).resolve() == args.fixture.resolve(),
            "The active workspace must be the explicitly chosen writable RC fixture.")
    workspace_id = inspected["workspace_id"]
    design = "low_pass_filter_lib:cell:schematic"
    require(design in call(sid, "design.list", workspace_id=workspace_id)["views"], "RC fixture not found.")
    target = {"workspace_id": workspace_id, "design": design}
    before = call(sid, "design.read", **target)
    resistor = next(item for item in before["instances"] if item["name"] == "R1")
    require(resistor["parameters"]["R"] in {"3 kOhm", "3.0 kOhm"}, "Start with a fixture containing R1 = 3 kOhm.")
    change = {"revision": before["revision"], "instance": "R1", "parameter": "R", "magnitude": 6,
              "unit": "kOhm", "request_id": uuid.uuid4().hex}
    changed = call(sid, "design.set_parameter", **target, **change)
    require(changed["state"] == "saved", str(changed))
    require(call(sid, "design.set_parameter", **target, **change) == changed, "Edit replay changed the result.")
    require(call(sid, "request.get", request_id=change["request_id"]) == changed, "Edit record mismatch.")
    try:
        call(sid, "design.set_parameter", **target, **(change | {"request_id": uuid.uuid4().hex}))
    except BridgeError as exc:
        require(exc.code == "design_changed", str(exc))
    else:
        raise RuntimeError("ADS accepted a stale design revision.")
    spec = target | {"revision": changed["revision"], "block": "AC1.AC",
                     "axis": "freq", "variable": "R1_v", "request_id": uuid.uuid4().hex}
    job = call(sid, "simulation.submit", **spec)
    require(call(sid, "simulation.submit", **spec)["job_id"] == job["job_id"], "Submit replay created a second job.")
    wait_for_job(sid, job["job_id"])
    result = asyncio.run(read_with_mcp(sid, job["job_id"], workspace_id))
    require(result["total_points"] == len(result["samples"]) == 31, "The AC sweep is incomplete.")
    error = max(abs(complex(real, imag) - 1 / (1 + 2j * math.pi * freq * 0.006))
                for freq, real, imag in result["samples"])
    require(error < 1e-6, f"Unexpected RC response. Error: {error}")
    print(f"ADS {inspected['ads_version']}, job {job['job_id']}, 31 samples, max complex error {error:.3g} V")


if __name__ == "__main__":
    main()
